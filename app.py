from flask import Flask, request, jsonify, send_file
import os, base64, io, time, requests

UPS_BASE = "https://wwwcie.ups.com"  # sandbox
CLIENT_ID     = os.getenv("UPS_CLIENT_ID")
CLIENT_SECRET = os.getenv("UPS_CLIENT_SECRET")
SHIPPER_NO    = os.getenv("UPS_SHIPPER_NUMBER", "1Y703V")
PROMO_CODE    = os.getenv("UPS_PROMO_CODE", "EIGSHIPSUPS")

app = Flask(__name__)
_token = {"value": None, "exp": 0}

def get_token():
    global _token
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = requests.post(
        f"{UPS_BASE}/security/v1/oauth/token",
        headers={"Authorization": "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
                 "Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"},
        data={"grant_type":"client_credentials"}, timeout=30
    )
    r.raise_for_status()
    j = r.json()
    _token = {"value": j["access_token"], "exp": time.time() + int(j.get("expires_in", 0))}
    return _token["value"]

@app.get("/health")
def health(): return {"status":"ok"}

@app.post("/labels/create")
def create_label():
    # Expect JSON {to:{name,addr1,city,state,zip,country,phone}, weight_oz}
    data = request.get_json(force=True)
    token = get_token()

    payload = {
      "ShipmentRequest": {
        "Request": {"TransactionReference": {"CustomerContext": "PRL"}},
        "Shipment": {
          "Shipper": {"Name":"First Impressions Dental Lab",
            "ShipperNumber": SHIPPER_NO,
            "Address":{"AddressLine":["700 N Neely St Suite 17"],"City":"Gilbert",
                       "StateProvinceCode":"AZ","PostalCode":"85233","CountryCode":"US"}},
          "ShipTo": {"Name": data["to"]["name"],
            "Address":{"AddressLine":[data["to"]["addr1"]],
                       "City":data["to"]["city"],"StateProvinceCode":data["to"]["state"],
                       "PostalCode":data["to"]["zip"],"CountryCode":data["to"]["country"]}},
          "Service":{"Code":"03","Description":"Ground"},
          "Package":[{"PackagingType":{"Code":"02"},
                      "PackageWeight":{"UnitOfMeasurement":{"Code":"LBS"},
                                       "Weight": max(1, round(float(data.get("weight_lbs",1)),2))}}],
          "PaymentInformation":{"ShipmentCharge":[{"Type":"01","BillShipper":{"AccountNumber":SHIPPER_NO}}]},
          "ReturnService":{"Code":"9"}  # PRL / Print Return Label
        },
        "LabelSpecification":{"LabelImageFormat":{"Code":"PDF"}}
      }
    }

    headers = {"Authorization": f"Bearer {token}",
               "Content-Type":"application/json",
               "Accept":"application/json",
               "transId":"prl-"+str(int(time.time())), "transactionSrc":"first-impressions"}
    # Newer UPS REST paths vary; this common one works in sandbox tenants:
    url = f"{UPS_BASE}/api/shipments/v1/shipments"
    r = requests.post(url, json=payload, headers=headers, timeout=45)
    if r.status_code >= 300:
        return jsonify({"ok": False, "status": r.status_code, "error": r.text}), r.status_code

    j = r.json()
    # Try common places where UPS returns the base64 label:
    b64 = (j.get("ShipmentResponse", {})
             .get("ShipmentResults", {})
             .get("PackageResults", [{}])[0]
             .get("ShippingLabel", {})
             .get("GraphicImage"))
    if not b64:
        return {"ok": False, "error":"Label not found in response", "raw": j}, 500

    pdf_bytes = base64.b64decode(b64)
    return send_file(io.BytesIO(pdf_bytes),
                     mimetype="application/pdf",
                     as_attachment=False,
                     download_name="return-label.pdf")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))