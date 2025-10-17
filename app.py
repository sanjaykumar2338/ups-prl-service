import os, base64, io, requests
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)

ALLOWED_ORIGINS = [
    "https://firstimpressions-dentallab.com",
    "https://www.firstimpressions-dentallab.com",
    "https://reward.easytechinfo.net",
]

CORS(
    app,
    resources={r"/*": {"origins": ALLOWED_ORIGINS}},   # or origins="*"
    supports_credentials=False,
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "transId", "transactionSrc"],
    expose_headers=["Content-Type"],
)

# ---- Config ----
UPS_ENV = os.getenv("UPS_ENV", "prod").lower()  # "sandbox" or "prod"
UPS_BASE = "https://wwwcie.ups.com" if UPS_ENV == "sandbox" else "https://onlinetools.ups.com"

CLIENT_ID      = os.getenv("UPS_CLIENT_ID")
CLIENT_SECRET  = os.getenv("UPS_CLIENT_SECRET")
SHIPPER_NO     = os.getenv("UPS_SHIPPER_NUMBER")   # e.g., 1Y703V
PROMO_CODE     = os.getenv("UPS_PROMO_CODE", "")   # optional

if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("Missing UPS_CLIENT_ID / UPS_CLIENT_SECRET in environment.")

# ---- Helpers ----
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
        # optional request identifiers
        "transId": "firstimpressions-prl",
        "transactionSrc": "firstimpressions-site",
    }

# ---- Routes ----
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
    return jsonify({
        "status": "UPS PRL microservice is live",
        "environment": UPS_ENV,
        "version": "1.1.0"
    })

@app.post("/labels/create")
def create_label():
    """
    Expects JSON like:
    {
      "to": { "name": "...", "addr1": "...", "city": "...", "state": "AZ", "zip": "...", "country": "US", "phone": "480..." },
      "weight_lbs": 1,
      "format": "PDF",       # optional: "PDF" or "GIF" (default PDF)
      "mode": "outbound",    # optional: "outbound" | "return" (default outbound)
      "reference": "Case #123"  # optional free-text
    }
    """
    try:
        body = request.get_json(force=True)
        if not body or "to" not in body:
            return {"ok": False, "error": "Missing 'to' address"}, 400

        if not SHIPPER_NO:
            return {"ok": False, "error": "Missing UPS_SHIPPER_NUMBER env"}, 500

        # ---- Hard-coded shipment defaults ----
        DIMENSIONS_IN = {"UnitOfMeasurement": {"Code": "IN"}, "Length": "6", "Width": "5", "Height": "5"}
        MERCHANDISE_DESCRIPTION = "Dental Products"
        DECLARED_VALUE = {"CurrencyCode": "USD", "MonetaryValue": "100"}

        fmt = str(body.get("format", "PDF")).upper()
        if fmt not in ("PDF", "GIF"):
            fmt = "PDF"

        service_code = body.get("service_code", "03")  # 03 = UPS® Ground

        # Who are we shipping to? (flip for return)
        mode = (body.get("mode") or "outbound").lower().strip()
        if mode not in ("outbound", "return"):
            mode = "outbound"

        # Patient address (from body)
        patient = {
            "Name": body["to"]["name"],
            "Address": {
                "AddressLine": [body["to"]["addr1"]],
                "City": body["to"]["city"],
                "StateProvinceCode": body["to"]["state"],
                "PostalCode": body["to"]["zip"],
                "CountryCode": body["to"]["country"]
            }
        }
        # Lab (shipper) constants
        lab = {
            "Name": "First Impressions Dental Lab",
            "ShipperNumber": SHIPPER_NO,
            "Address": {
                "AddressLine": ["125 W Broadway St"],
                "City": "Mesa",
                "StateProvinceCode": "AZ",
                "PostalCode": "85210",
                "CountryCode": "US"
            }
        }

        # Build base Shipment
        shipment = {
            "Description": MERCHANDISE_DESCRIPTION,  # hard-coded merch/contents
            "Shipper": lab,
            "PaymentInformation": {
                "ShipmentCharge": {
                    "Type": "01",  # Transportation
                    "BillShipper": {"AccountNumber": SHIPPER_NO}
                }
            },
            "Service": {"Code": service_code},
            "Package": {
                "Packaging": {"Code": "02"},  # Customer-supplied package
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "LBS"},
                    "Weight": str(body.get("weight_lbs", 1))
                },
                # --- Hard-coded dimensions and declared value ---
                "Dimensions": DIMENSIONS_IN,
                "PackageServiceOptions": {
                    "DeclaredValue": DECLARED_VALUE
                }
            }
        }

        # Reference (optional): attach at Shipment level
        ref_val = (body.get("reference") or "").strip()
        if ref_val:
            shipment["ReferenceNumber"] = [{"Code": "PO", "Value": ref_val}]

        # Outbound vs Return address wiring
        if mode == "outbound":
            # Lab -> Patient
            shipment["ShipTo"] = patient
            # Optional explicit ShipFrom = lab
            shipment["ShipFrom"] = {
                "Name": lab["Name"],
                "Address": lab["Address"]
            }
            filename_prefix = "shipping-label"
        else:
            # Return: Patient -> Lab
            shipment["ShipTo"] = {
                "Name": lab["Name"],
                "Address": lab["Address"]
            }
            shipment["ShipFrom"] = patient
            filename_prefix = "return-label"

        ship_request = {
            "ShipmentRequest": {
                "Request": {"RequestOption": "nonvalidate"},
                "Shipment": shipment,
                "LabelSpecification": {"LabelImageFormat": {"Code": fmt}}
            }
        }

        url = f"{UPS_BASE}/api/shipments/v2409/ship"
        r = requests.post(url, headers=ups_headers(), json=ship_request, timeout=45)

        if r.status_code >= 300:
            return {"ok": False, "status": r.status_code, "error": r.text}, r.status_code

        data = r.json()
        pkg = data["ShipmentResponse"]["ShipmentResults"]["PackageResults"]
        if isinstance(pkg, list):
            pkg = pkg[0]

        label_b64 = pkg["ShippingLabel"]["GraphicImage"]
        tracking  = pkg.get("TrackingNumber")

        file_bytes = base64.b64decode(label_b64)
        ext = "pdf" if fmt == "PDF" else "gif"
        filename = f"{filename_prefix}.{ext}"

        if request.args.get("json") == "1":
            return {"ok": True, "tracking": tracking, "label_base64": label_b64, "format": fmt}

        return send_file(
            io.BytesIO(file_bytes),
            mimetype=("application/pdf" if fmt == "PDF" else "image/gif"),
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

# ---- Entrypoint ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))