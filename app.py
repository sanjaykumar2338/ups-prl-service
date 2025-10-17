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
    Request JSON:
    {
      "to": { "name": "...", "addr1": "...", "city": "...", "state": "AZ", "zip": "...", "country": "US", "phone": "480..." },
      "weight_lbs": 1,
      "format": "PDF",       # optional: PDF|GIF (default PDF)
      "mode": "outbound",    # optional: outbound|return (default outbound)
      "reference": "Case #123"   # optional
    }

    Response:
      - file download (PDF/GIF) by default
      - JSON when query param ?json=1 is present
    """
    try:
        body = request.get_json(force=True) or {}
        if "to" not in body:
            return {"ok": False, "error": "Missing 'to' address"}, 400
        if not SHIPPER_NO:
            return {"ok": False, "error": "Missing UPS_SHIPPER_NUMBER env"}, 500

        # --- Hard-coded shipment defaults (per client) ---
        DIMENSIONS_IN = {"UnitOfMeasurement": {"Code": "IN"}, "Length": "6", "Width": "5", "Height": "5"}
        MERCHANDISE_DESCRIPTION = "Dental Products"
        DECLARED_VALUE = {"CurrencyCode": "USD", "MonetaryValue": "100"}

        # Label format
        fmt = str(body.get("format", "PDF")).upper()
        if fmt not in ("PDF", "GIF"):
            fmt = "PDF"

        service_code = str(body.get("service_code", "03"))  # 03 = UPS® Ground

        # Mode (direction)
        mode = (body.get("mode") or "outbound").strip().lower()
        if mode not in ("outbound", "return"):
            mode = "outbound"

        # Patient/office address (from request)
        to_json = body["to"]
        patient = {
            "Name": (to_json.get("name") or "").strip() or "Recipient",
            "Address": {
                "AddressLine": [to_json.get("addr1", "")],
                "City": to_json.get("city", ""),
                "StateProvinceCode": to_json.get("state", ""),
                "PostalCode": to_json.get("zip", ""),
                "CountryCode": to_json.get("country", "US"),
            }
        }
        phone = (to_json.get("phone") or "").strip()
        if phone:
            patient["Phone"] = {"Number": phone}

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

        # Build Shipment skeleton
        shipment = {
            "Description": MERCHANDISE_DESCRIPTION,
            "Shipper": lab,  # account billed
            "PaymentInformation": {
                "ShipmentCharge": {
                    "Type": "01",  # Transportation
                    "BillShipper": {"AccountNumber": SHIPPER_NO}
                }
            },
            "Service": {"Code": service_code},
            "Package": {
                "Packaging": {"Code": "02"},  # Customer-supplied
                "PackageWeight": {
                    "UnitOfMeasurement": {"Code": "LBS"},
                    "Weight": str(body.get("weight_lbs", 1))
                },
                "Dimensions": DIMENSIONS_IN,
                "PackageServiceOptions": {"DeclaredValue": DECLARED_VALUE}
            }
        }

        # === Address wiring per mode ===
        if mode == "outbound":
            # Lab → Patient
            shipment["ShipFrom"] = {"Name": lab["Name"], "Address": lab["Address"]}
            shipment["ShipTo"]   = patient
            filename_prefix = "shipping-label"
        else:
            # Return: Dental Office (user) → Lab
            shipment["ShipFrom"] = patient
            shipment["ShipTo"]   = {"Name": lab["Name"], "Address": lab["Address"]}
            filename_prefix = "return-label"

        # === References at PACKAGE level (Shipment-level may be disallowed -> 120541) ===
        pkg_refs = []
        # User-provided case/patient reference (optional)
        ref_val = (body.get("reference") or "").strip()
        if ref_val:
            pkg_refs.append({"Code": "PO", "Value": ref_val})
        # Always tag promo for auditing
        if PROMO_CODE:
            pkg_refs.append({"Code": "PM", "Value": f"Promo:{PROMO_CODE}"})
        if pkg_refs:
            # Some accounts accept array; if 400 occurs with code about array, switch to single object.
            shipment["Package"]["ReferenceNumber"] = pkg_refs

        # Build request
        ship_request = {
            "ShipmentRequest": {
                "Request": {"RequestOption": "nonvalidate"},
                "Shipment": shipment,
                "LabelSpecification": {"LabelImageFormat": {"Code": fmt}}
            }
        }

        # Call UPS
        url = f"{UPS_BASE}/api/shipments/v2409/ship"
        resp = requests.post(url, headers=ups_headers(), json=ship_request, timeout=45)

        # Bubble real UPS errors
        if resp.status_code >= 300:
            return {"ok": False, "status": resp.status_code, "error": resp.text}, resp.status_code

        data = resp.json()
        # Normalize PackageResults shape (object vs array)
        pkg = data["ShipmentResponse"]["ShipmentResults"].get("PackageResults")
        if isinstance(pkg, list):
            pkg = pkg[0]

        label_b64 = pkg["ShippingLabel"]["GraphicImage"]
        tracking  = pkg.get("TrackingNumber")

        # Optional JSON response
        if request.args.get("json") == "1":
            return {"ok": True, "tracking": tracking, "label_base64": label_b64, "format": fmt}

        # File download
        ext = "pdf" if fmt == "PDF" else "gif"
        filename = f"{filename_prefix}.{ext}"
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