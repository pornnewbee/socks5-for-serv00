import os
import requests
from tqdm import tqdm

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]
WORKER_NAME = sub

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

def fetch_logs():
    cursor = None
    all_logs = []

    while True:
        payload = {
            "timeframe": {
                "since": "2026-02-01T00:00:00Z",
                "until": "2026-02-02T00:00:00Z"
            },
            "filter": f'Worker == "{WORKER_NAME}"',
            "limit": 100
        }

        if cursor:
            payload["cursor"] = cursor

        resp = requests.post(API_URL, headers=headers, json=payload)
        data = resp.json()

        if not data.get("success"):
            print("Error:", data)
            break

        results = data.get("result", {}).get("data", [])
        all_logs.extend(results)

        cursor = data.get("result", {}).get("meta", {}).get("nextCursor")
        if not cursor:
            break

    return all_logs

if __name__ == "__main__":
    logs = fetch_logs()
    print(f"Fetched {len(logs)} logs for Worker '{WORKER_NAME}'")
    # 保存到文件
    with open(f"{WORKER_NAME}_logs.json", "w", encoding="utf-8") as f:
        import json
        json.dump(logs, f, ensure_ascii=False, indent=2)
