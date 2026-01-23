import argparse
import requests


def send_json(error_id, equipment_id, anomaly_type, resolved_by, resolution_notes):
    url = "https://127.0.0.1:443/resolvealarms"
    payload = {
        "detected_anomaly_type": anomaly_type,
        "equipment_id": equipment_id,
        "resolved_by": resolved_by,
        "resolution_notes": resolution_notes,
    }
    if error_id is not None:
        payload["error_id"] = error_id

    try:
        resp = requests.post(url, json=payload, timeout=5, verify=False)
        resp.raise_for_status()
        print("Response:", resp.status_code, resp.text)
    except requests.RequestException as e:
        print("Request failed:", e)
        return


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve the latest unresolved alert.")
    parser.add_argument("--error-id", type=int)
    parser.add_argument("--equipment-id", default="EQ003")
    parser.add_argument("--anomaly-type", default="轉速過低")
    parser.add_argument("--resolved-by", default="user001")
    parser.add_argument("--resolution-notes", default="")
    args = parser.parse_args()

    send_json(
        error_id=args.error_id,
        equipment_id=args.equipment_id,
        anomaly_type=args.anomaly_type,
        resolved_by=args.resolved_by,
        resolution_notes=args.resolution_notes,
    )
