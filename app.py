import os, base64, time, requests
from flask import Flask, request, jsonify, send_file
from dotenv import load_dotenv

load_dotenv()  # <-- loads .env when running locally
app = Flask(__name__)

UPS_BASE = "https://wwwcie.ups.com"  # sandbox
CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
SHIPPER_NO  = os.getenv("UPS_SHIPPER_NUMBER", "1Y703V")
PROMO_CODE  = os.getenv("UPS_PROMO_CODE", "EIGSHIPSUPS")

app = Flask(__name__)
_token = {"value": None, "exp": 0}

def get_token():
    if not CLIENT_ID or not CLIENT_SECRET:
        raise RuntimeError("UPS_CLIENT_ID/UPS_CLIENT_SECRET not set")

    token_url = f"{UPS_BASE}/security/v1/oauth/token"
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json"
    }
    data = {"grant_type": "client_credentials"}

    r = requests.post(token_url, headers=headers, data=data, timeout=30)
    if r.status_code >= 300:
        # surface the real UPS error to your logs
        raise RuntimeError(f"UPS token HTTP {r.status_code}: {r.text}")
    j = r.json()
    return j["access_token"], int(j.get("expires_in", 0))

@app.get("/token-test")
def token_test():
    try:
        tok, ttl = get_token()
        return {"ok": True, "ttl": ttl, "preview": tok[:20] + "..."}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.get("/health")
def health(): return {"status":"ok"}

@app.route('/')
def index():
    return jsonify({
        "status": "UPS PRL microservice is live",
        "environment": "production",
        "version": "1.0.0"
    })

@app.post("/labels/create")
def create_label():
    try:
        payload = request.get_json(force=True)
        if not payload or "to" not in payload:
            return {"ok": False, "error": "Missing required fields"}, 400

        token, _ = get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Example UPS shipment request
        ship_request = {
            "ShipmentRequest": {
                "Request": {"RequestOption": "nonvalidate"},
                "Shipment": {
                    "Description": "Dental Lab Return Label",
                    "Shipper": {
                        "Name": "First Impressions Dental Lab",
                        "Address": {
                            "AddressLine": ["125 W Broadway St"],
                            "City": "Mesa",
                            "StateProvinceCode": "AZ",
                            "PostalCode": "85210",
                            "CountryCode": "US"
                        }
                    },
                    "ShipTo": {
                        "Name": payload["to"]["name"],
                        "Address": {
                            "AddressLine": [payload["to"]["addr1"]],
                            "City": payload["to"]["city"],
                            "StateProvinceCode": payload["to"]["state"],
                            "PostalCode": payload["to"]["zip"],
                            "CountryCode": payload["to"]["country"]
                        }
                    },
                    "Package": {
                        "PackagingType": {"Code": "02"},
                        "PackageWeight": {
                            "UnitOfMeasurement": {"Code": "LBS"},
                            "Weight": str(payload.get("weight_lbs", 1))
                        }
                    }
                },
                "LabelSpecification": {
                    "LabelImageFormat": {"Code": "GIF"}
                }
            }
        }

        ups_url = f"{UPS_BASE}/api/shipments/v1/ship"
        resp = requests.post(ups_url, headers=headers, json=ship_request, timeout=30)

        if resp.status_code >= 300:
            return {"ok": False, "error": resp.text}, resp.status_code

        data = resp.json()
        label_b64 = data["ShipmentResponse"]["ShipmentResults"]["PackageResults"]["ShippingLabel"]["GraphicImage"]

        import base64, io
        label_bytes = base64.b64decode(label_b64)
        return send_file(
            io.BytesIO(label_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="return-label.pdf"
        )

    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))