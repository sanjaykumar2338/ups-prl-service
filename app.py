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
    try:
        body = request.get_json(force=True) or {}
        if "to" not in body:
            return {"ok": False, "error": "Missing 'to' address"}, 400
        if not SHIPPER_NO:
            return {"ok": False, "error": "Missing UPS_SHIPPER_NUMBER env"}, 500

        # --- Defaults ---
        DIMENSIONS_IN = {"UnitOfMeasurement": {"Code": "IN"}, "Length": "6", "Width": "5", "Height": "5"}
        DECLARED_VALUE = {"CurrencyCode": "USD", "MonetaryValue": "100"}
        fmt = (body.get("format") or "PDF").upper()
        service_code = "03"

        # --- Sender (dental office) ---
        to_json = body["to"]
        sender_name = (to_json.get("name") or "").strip() or "Sender"
        sender_addr = f"{to_json.get('addr1','')}, {to_json.get('city','')}, {to_json.get('state','')} {to_json.get('zip','')}"
        sender_phone = (to_json.get("phone") or "").strip()

        ship_from = {
            "Name": sender_name,
            "CompanyName": sender_name,
            "AttentionName": sender_name,
            "Address": {
                "AddressLine": [to_json.get("addr1", "")],
                "City": to_json.get("city", ""),
                "StateProvinceCode": to_json.get("state", ""),
                "PostalCode": to_json.get("zip", ""),
                "CountryCode": to_json.get("country", "US"),
            },
        }
        if sender_phone:
            ship_from["Phone"] = {"Number": sender_phone}

        # --- Lab info ---
        LAB_ZIP = "85233"
        lab_addr = {
            "AddressLine": ["700 North Neely Street, Ste. #17"],
            "City": "Gilbert",
            "StateProvinceCode": "AZ",
            "PostalCode": LAB_ZIP,
            "CountryCode": "US",
        }

        # --- Shipment (BillShipper, PRL) ---
        shipment = {
            "Description": "Dental Products",
            "Shipper": {
                "ShipperNumber": SHIPPER_NO,
                "Name": "First Impressions Dental Lab",
                "Address": lab_addr,
            },
            "PaymentInformation": {
                "ShipmentCharge": {"Type": "01", "BillShipper": {"AccountNumber": SHIPPER_NO}}
            },
            "Service": {"Code": service_code},
            "ShipmentServiceOptions": {"ReturnService": {"Code": "02"}},
            "ShipFrom": ship_from,
            "ShipTo": {"Name": "First Impressions Dental Lab", "Address": lab_addr},
            "Package": {
                "Packaging": {"Code": "02"},
                "PackageWeight": {"UnitOfMeasurement": {"Code": "LBS"}, "Weight": str(body.get("weight_lbs", 1))},
                "Dimensions": DIMENSIONS_IN,
                "PackageServiceOptions": {"DeclaredValue": DECLARED_VALUE},
            },
        }

        # --- References (visible in panel) ---
        refs = []
        # User reference (cleaned)
        user_ref = _clean_ref(body.get("reference"))
        if user_ref:
            refs.append({"Code": "PO", "Value": user_ref})
        else:
            # fall back to sender name (cleaned)
            refs.append({"Code": "PO", "Value": _clean_ref(sender_name)})

        # Promo tag (no colon)
        if PROMO_CODE:
            promo_ref = _clean_ref(f"Promo {PROMO_CODE}")
            if promo_ref:
                refs.append({"Code": "PM", "Value": promo_ref})

        shipment["Package"]["ReferenceNumber"] = refs


        ship_request = {
            "ShipmentRequest": {
                "Request": {"RequestOption": "nonvalidate"},
                "Shipment": shipment,
                "LabelSpecification": {"LabelImageFormat": {"Code": fmt}},
            }
        }

        # --- UPS API call ---
        url = f"{UPS_BASE}/api/shipments/v2409/ship"
        resp = requests.post(url, headers=ups_headers(), json=ship_request, timeout=45)
        if resp.status_code >= 300:
            return {"ok": False, "status": resp.status_code, "error": resp.text}, resp.status_code

        data = resp.json()
        pkg = data["ShipmentResponse"]["ShipmentResults"].get("PackageResults")
        if isinstance(pkg, list):
            pkg = pkg[0]
        label_b64 = pkg["ShippingLabel"]["GraphicImage"]
        tracking = pkg.get("TrackingNumber")

        # --- Decode label + overlay custom sender note ---
        label_pdf = base64.b64decode(label_b64)
        label_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        overlay_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        label_temp.write(label_pdf)
        label_temp.close()

        c = canvas.Canvas(overlay_temp.name, pagesize=letter)
        c.setFillColorRGB(0.9, 0.9, 0.9)     # light gray background
        c.rect(320, 730, 220, 18, fill=True, stroke=False)
        c.setFillColorRGB(0, 0, 0)           # switch text back to black
        c.setFont("Helvetica-Bold", 9)
        text = f"From: {sender_name} | {sender_addr}"
        text_width = c.stringWidth(text, "Helvetica-Bold", 9)
        x_center = (letter[0] - text_width) / 2
        c.drawString(x_center, 740, text)
        c.save()

        overlay = PdfReader(overlay_temp.name)
        base = PdfReader(label_temp.name)
        writer = PdfWriter()
        page = base.pages[0]
        page.merge_page(overlay.pages[0])
        writer.add_page(page)

        final_pdf = io.BytesIO()
        writer.write(final_pdf)
        final_pdf.seek(0)

        if request.args.get("json") == "1":
            return {"ok": True, "tracking": tracking, "format": fmt}

        return send_file(final_pdf, mimetype="application/pdf", as_attachment=True, download_name="return-label.pdf")

    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ===== Entrypoint =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))