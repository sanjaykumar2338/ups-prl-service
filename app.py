import os, io, base64, requests, tempfile
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from PyPDF2 import PdfReader, PdfWriter
import re
# ===== Load env =====
load_dotenv()

# ===== App & CORS =====
app = Flask(__name__)

ALLOWED_ORIGINS = [
    "https://firstimpressions-dentallab.com",
    "https://www.firstimpressions-dentallab.com",
    "https://reward.easytechinfo.net",
]
CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},
    supports_credentials=False,
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "transId", "transactionSrc"],
    expose_headers=["Content-Type"],
)

# ===== Config =====
UPS_ENV   = os.getenv("UPS_ENV", "prod").lower()     # "sandbox" or "prod"
UPS_BASE  = "https://wwwcie.ups.com" if UPS_ENV == "sandbox" else "https://onlinetools.ups.com"
CLIENT_ID = os.getenv("UPS_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
SHIPPER_NO = os.getenv("UPS_SHIPPER_NUMBER")  # UPS account number used for PRL billing
PROMO_CODE = os.getenv("UPS_PROMO_CODE", "EIGSHIPSUPS")
LAB_NAME = "First Impressions Dental Lab"
LAB_ADDRESS = {
    "AddressLine": ["701 W. Southern Ave", "#104"],
    "City": "Mesa",
    "StateProvinceCode": "AZ",
    "PostalCode": "85210",
    "CountryCode": "US",
}

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("Missing UPS_CLIENT_ID / UPS_CLIENT_SECRET in environment.")

_REF_ALLOWED = re.compile(r"[^A-Za-z0-9 \-._/]+")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
def _clean_ref(s: str, maxlen: int = 35) -> str:
    return _REF_ALLOWED.sub("", (s or "")).strip()[:maxlen]

def _clean_text(value) -> str:
    value = "" if value is None else str(value)
    value = _CONTROL_CHARS.sub(" ", value.replace("\r", " ").replace("\n", " "))
    return re.sub(r"\s+", " ", value).strip()

def _first_present(payload: dict, *keys: str):
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return ""

def _build_address_lines(line1, line2=""):
    return [line for line in (_clean_text(line1), _clean_text(line2)) if line]

def _format_address_for_note(address_lines, city, state, postal_code):
    state_postal = " ".join(part for part in (_clean_text(state), _clean_text(postal_code)) if part)
    locality = ", ".join(part for part in (_clean_text(city), state_postal) if part)
    parts = list(address_lines)
    if locality:
        parts.append(locality)
    return ", ".join(parts)

def _fit_overlay_text(pdf_canvas, text, max_width, font_name="Helvetica-Bold", start_size=10, min_size=7):
    size = start_size
    while size > min_size and pdf_canvas.stringWidth(text, font_name, size) > max_width:
        size -= 0.5

    pdf_canvas.setFont(font_name, size)
    if pdf_canvas.stringWidth(text, font_name, size) <= max_width:
        return text

    ellipsis = "..."
    shortened = text
    while shortened and pdf_canvas.stringWidth(shortened + ellipsis, font_name, size) > max_width:
        shortened = shortened[:-1]
    return (shortened.rstrip() + ellipsis) if shortened else ellipsis

# ===== OAuth helpers =====
def get_token():
    """Client Credentials OAuth2 (no user login)"""
    token_url = f"{UPS_BASE}/security/v1/oauth/token"
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {"grant_type": "client_credentials"}
    r = requests.post(token_url, headers=headers, data=data, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"UPS token HTTP {r.status_code}: {r.text}")
    j = r.json()
    return j["access_token"], int(j.get("expires_in", 0))

def ups_headers():
    tok, _ = get_token()
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "transId": "firstimpressions-prl",
        "transactionSrc": "firstimpressions-site",
    }

# ===== Routes =====
@app.get("/health")
def health():
    return {"status": "ok", "env": UPS_ENV, "base": UPS_BASE}

@app.get("/token-test")
def token_test():
    try:
        tok, ttl = get_token()
        return {"ok": True, "ttl": ttl, "preview": tok[:24] + "...", "env": UPS_ENV}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/")
def root():
    return jsonify({"status": "UPS PRL microservice is live", "environment": UPS_ENV, "version": "1.2.0"})

@app.post("/labels/create")
def create_label():
    import re, tempfile, base64, io, os, requests
    from flask import request, send_file
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    from PyPDF2 import PdfReader, PdfWriter

    _REF_ALLOWED = re.compile(r"[^A-Za-z0-9 \-._/]+")
    def _clean_ref(s, n=35): return _REF_ALLOWED.sub("", (s or "")).strip()[:n]

    try:
        body = request.get_json(force=True) or {}
        if "to" not in body: return {"ok": False, "error": "Missing 'to' address"}, 400
        if not SHIPPER_NO:  return {"ok": False, "error": "Missing UPS_SHIPPER_NUMBER env"}, 500

        fmt = (body.get("format") or "PDF").upper()
        if fmt not in ("PDF","GIF"): fmt = "PDF"

        # sender (customer)
        to = body["to"]
        sender_name = _clean_text(_first_present(to, "name")) or "Sender"
        sender_addr1 = _first_present(to, "addr1", "address1", "address_line_1", "addressLine1")
        sender_addr2 = _first_present(to, "addr2", "address2", "address_line_2", "addressLine2")
        sender_address_lines = _build_address_lines(sender_addr1, sender_addr2)
        sender_city = _clean_text(_first_present(to, "city"))
        sender_state = _clean_text(_first_present(to, "state", "state_code", "stateCode")).upper()
        sender_zip = _clean_text(_first_present(to, "zip", "postal_code", "postalCode"))
        sender_country = (_clean_text(_first_present(to, "country", "country_code", "countryCode")) or "US").upper()
        sender_phone = _clean_text(_first_present(to, "phone"))
        sender_addr = _format_address_for_note(sender_address_lines, sender_city, sender_state, sender_zip)
        ship_from = {
            "Name": sender_name,
            "CompanyName": sender_name,
            "AttentionName": sender_name,
            "Address": {
                "AddressLine": sender_address_lines or [""],
                "City": sender_city,
                "StateProvinceCode": sender_state,
                "PostalCode": sender_zip,
                "CountryCode": sender_country,
            }
        }
        if sender_phone:
            ship_from["Phone"] = {"Number": sender_phone}

        # lab (destination)
        # Keep the destination address lines in UPS street-then-suite order.
        lab_street, lab_suite = LAB_ADDRESS["AddressLine"]
        lab_addr = {**LAB_ADDRESS, "AddressLine": [lab_street, lab_suite]}

        shipment = {
            "Description": "Dental Products",

            # IMPORTANT: put lab account on ShipperNumber (satisfies 120100)
            # but keep the sender's address/name so "Ship From" reflects customer.
            "Shipper": {
                "ShipperNumber": SHIPPER_NO,
                **ship_from
            },

            "PaymentInformation": {
                "ShipmentCharge": {
                    "Type": "01",
                    "BillShipper": { "AccountNumber": SHIPPER_NO }   # avoid 120412
                }
            },

            "Service": {"Code": "03"},
            "ShipmentServiceOptions": {"ReturnService": {"Code": "02"}},  # PRL
            "ShipFrom": ship_from,
            "ShipTo": { "Name": LAB_NAME, "Address": lab_addr },

            "Package": {
                "Packaging": {"Code": "02"},
                "PackageWeight": {"UnitOfMeasurement": {"Code": "LBS"}, "Weight": str(body.get("weight_lbs", 1))},
                "Dimensions": {"UnitOfMeasurement":{"Code":"IN"}, "Length":"6","Width":"5","Height":"5"},
                "PackageServiceOptions": {"DeclaredValue": {"CurrencyCode":"USD","MonetaryValue":"100"}}
            }
        }

        # references (sanitized)
        refs = [{"Code":"PO","Value": _clean_ref(body.get("reference")) or _clean_ref(sender_name)}]
        if PROMO_CODE:
            pr = _clean_ref(f"Promo {PROMO_CODE}")
            if pr: refs.append({"Code":"PM","Value":pr})
        shipment["Package"]["ReferenceNumber"] = refs

        ship_request = {
            "ShipmentRequest": {
                "Request": {"RequestOption": "nonvalidate"},
                "Shipment": shipment,
                "LabelSpecification": {"LabelImageFormat": {"Code": fmt}}
            }
        }

        url = f"{UPS_BASE}/api/shipments/v2409/ship"
        resp = requests.post(url, headers=ups_headers(), json=ship_request, timeout=45)
        if resp.status_code >= 300:
            return {"ok": False, "status": resp.status_code, "error": resp.text}, resp.status_code

        data = resp.json()
        pkg = data["ShipmentResponse"]["ShipmentResults"].get("PackageResults")
        if isinstance(pkg, list): pkg = pkg[0]
        tracking  = pkg.get("TrackingNumber") or ""
        label_b64 = pkg["ShippingLabel"]["GraphicImage"]

        # overlay note on right edge (bottom when rotated)
        base_pdf = base64.b64decode(label_b64)
        t_label = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        t_overlay = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        t_merged = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        try:
            with open(t_label.name,"wb") as f: f.write(base_pdf)

            c = canvas.Canvas(t_overlay.name, pagesize=letter)
            pw, ph = letter

            c.saveState()
            c.translate(pw, 0)
            c.rotate(90)

            # Draw background bar slightly above bottom edge to prevent clipping
            c.setFillColorRGB(0.15, 0.15, 0.15)
            c.rect(0, 5, ph, 25, fill=1, stroke=0)

            # Draw centered white text
            c.setFillColorRGB(1, 1, 1)
            note = f"FROM: {sender_name}"
            if sender_addr:
                note = f"{note} • {sender_addr}"
            note = _fit_overlay_text(c, note, ph - 36)
            c.drawString(18, 13, note)  # shifted up a bit for perfect vertical centering

            c.restoreState()
            c.save()

            base = PdfReader(t_label.name); over = PdfReader(t_overlay.name)
            page = base.pages[0]; page.merge_page(over.pages[0])
            w = PdfWriter(); w.add_page(page)
            with open(t_merged.name,"wb") as f: w.write(f)

            if request.args.get("json") == "1":
                with open(t_merged.name,"rb") as f:
                    return {"ok": True, "tracking": tracking, "format": fmt,
                            "label_base64": base64.b64encode(f.read()).decode()}

            with open(t_merged.name,"rb") as f: final_bytes = f.read()
            return send_file(io.BytesIO(final_bytes),
                             mimetype=("application/pdf" if fmt=="PDF" else "image/gif"),
                             as_attachment=True,
                             download_name=("return-label.pdf" if fmt=="PDF" else "return-label.gif"))
        finally:
            for p in (t_label.name, t_overlay.name, t_merged.name):
                try: os.unlink(p)
                except: pass
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ===== Entrypoint =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
