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
SHIPPER_NO = os.getenv("UPS_SHIPPER_NUMBER")  # e.g., 1Yxxxx
PROMO_CODE = os.getenv("UPS_PROMO_CODE", "EIGSHIPSUPS")

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("Missing UPS_CLIENT_ID / UPS_CLIENT_SECRET in environment.")

_REF_ALLOWED = re.compile(r"[^A-Za-z0-9 \-._/]+")
def _clean_ref(s: str, maxlen: int = 35) -> str:
    return _REF_ALLOWED.sub("", (s or "")).strip()[:maxlen]

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
        sender_name  = (to.get("name") or "").strip() or "Sender"
        sender_addr  = f"{to.get('addr1','')}, {to.get('city','')}, {to.get('state','')} {to.get('zip','')}"
        ship_from = {
            "Name": sender_name,
            "CompanyName": sender_name,
            "AttentionName": sender_name,
            "Address": {
                "AddressLine": [to.get("addr1","")],
                "City": to.get("city",""),
                "StateProvinceCode": to.get("state",""),
                "PostalCode": to.get("zip",""),
                "CountryCode": to.get("country","US"),
            }
        }
        if (to.get("phone") or "").strip():
            ship_from["Phone"] = {"Number": to["phone"].strip()}

        # lab (destination)
        LAB_ZIP = "85233"
        lab_addr = {
            "AddressLine": ["700 North Neely Street, Ste. #17"],
            "City": "Gilbert",
            "StateProvinceCode": "AZ",
            "PostalCode": LAB_ZIP,
            "CountryCode": "US",
        }

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
            "ShipTo": { "Name": "First Impressions Dental Lab", "Address": lab_addr },

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
            c.setFont("Helvetica-Bold", 10)
            note = f"FROM: {sender_name} • {sender_addr}"[:110]
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
