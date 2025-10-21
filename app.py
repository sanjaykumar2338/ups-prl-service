import os, io, base64, requests
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from dotenv import load_dotenv

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
    """
    UPS Print Return Label (PRL) with single UPS account:
      - Billing: BillShipper (lab account)  -> avoids 120412
      - ShipFrom: sender (dental office)    -> captured in API/manifest
      - ShipTo:   First Impressions Dental Lab (Gilbert)
      - References: include sender + promo so it's visible on label panel
    """
    try:
        body = request.get_json(force=True) or {}
        if "to" not in body:
            return {"ok": False, "error": "Missing 'to' address"}, 400
        if not SHIPPER_NO:
            return {"ok": False, "error": "Missing UPS_SHIPPER_NUMBER env"}, 500

        # ---- Defaults ----
        DIMENSIONS_IN = {"UnitOfMeasurement": {"Code": "IN"}, "Length": "6", "Width": "5", "Height": "5"}
        MERCHANDISE_DESCRIPTION = "Dental Products"
        DECLARED_VALUE = {"CurrencyCode": "USD", "MonetaryValue": "100"}
        fmt = (body.get("format") or "PDF").upper()
        if fmt not in ("PDF", "GIF"):
            fmt = "PDF"
        service_code = "03"  # UPS Ground

        # ---- Sender (dental office) ----
        to_json = body["to"]
        sender_name  = (to_json.get("name") or "").strip() or "Sender"
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
                "CountryCode": to_json.get("country", "US")
            }
        }
        if sender_phone:
            ship_from["Phone"] = {"Number": sender_phone}

        # ---- Lab (billed shipper + destination) ----
        LAB_ZIP = "85233"
        lab_addr = {
            "AddressLine": ["700 North Neely Street, Ste. #17"],
            "City": "Gilbert",
            "StateProvinceCode": "AZ",
            "PostalCode": LAB_ZIP,
            "CountryCode": "US"
        }

        # ---- Shipment (PRL, BillShipper) ----
        shipment = {
            "Description": MERCHANDISE_DESCRIPTION,

            # Shipper = lab account (BillShipper requires this)
            "Shipper": {
                "ShipperNumber": SHIPPER_NO,
                "Name": "First Impressions Dental Lab",
                "AttentionName": "Shipping",
                "Address": lab_addr
            },

            "PaymentInformation": {
                "ShipmentCharge": {
                    "Type": "01",  # Transportation
                    "BillShipper": { "AccountNumber": SHIPPER_NO }
                }
            },

            "Service": {"Code": service_code},
            "ShipmentServiceOptions": { "ReturnService": { "Code": "02" } },  # PRL
            "ShipFrom": ship_from,  # captured in API/manifest; may not print in From block
            "ShipTo": {
                "Name": "First Impressions Dental Lab",
                "AttentionName": "Receiving",
                "Address": lab_addr
            },

            "Package": {
                "Packaging": {"Code": "02"},
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "LBS"},
                    "Weight": str(body.get("weight_lbs", 1))
                },
                "Dimensions": DIMENSIONS_IN,
                "PackageServiceOptions": {"DeclaredValue": DECLARED_VALUE}
            }
        }

        # ---- References (make sender visible on label panel) ----
        refs = []
        ref_val = (body.get("reference") or "").strip()
        # Put sender name in PO reference for easy ID on label/tracking
        sender_tag = sender_name if sender_name else "Sender"
        if ref_val:
            refs.append({"Code": "PO", "Value": f"{sender_tag} | {ref_val}"})
        else:
            refs.append({"Code": "PO", "Value": sender_tag})
        promo = os.getenv("UPS_PROMO_CODE", "EIGSHIPSUPS")
        if promo:
            refs.append({"Code": "PM", "Value": f"Promo:{promo}"})
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
        if isinstance(pkg, list):
            pkg = pkg[0]
        label_b64 = pkg["ShippingLabel"]["GraphicImage"]
        tracking  = pkg.get("TrackingNumber")

        if request.args.get("json") == "1":
            return {"ok": True, "tracking": tracking, "label_base64": label_b64, "format": fmt}

        ext = "pdf" if fmt == "PDF" else "gif"
        filename = f"return-label.{ext}"
        file_bytes = base64.b64decode(label_b64)
        return send_file(
            io.BytesIO(file_bytes),
            mimetype=("application/pdf" if fmt == "PDF" else "image/gif"),
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ===== Entrypoint =====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))