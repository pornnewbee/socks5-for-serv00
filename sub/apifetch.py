import os
import requests
from datetime import datetime, timedelta, timezone
import json

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]

QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"

API_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

headers = {
    "Authorization": f"Bearer {CF_API_TOKEN}",
    "Content-Type": "application/json"
}

def get_utc_timeframe(days=7):
    now = datetime.now(timezone.utc)

    start = (now - timedelta(days=days-1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    end = now.replace(
        hour=23, minute=59, second=59, microsecond=0
    )

    return int(start.timestamp()*1000), int(end.timestamp()*1000)

def dry_run_check(since, until):
    payload = {
        "queryId": QUERY_ID,
        "dry": True,
        "timeframe": {
            "from": since,
            "to": until
        }
    }

    r = requests.post(API_URL, headers=headers, json=payload)
    print("Dry run result:", r.text)

def fetch_logs():
    since, until = get_utc_timeframe()

    dry_run_check(since, until)

    cursor = None
    all_logs = []

    while True:
        payload = {
            "queryId": QUERY_ID,
            "timeframe": {
                "from": since,
                "to": until
            },
            "limit": 100
        }

        if cursor:
            payload["cursor"] = cursor

        r = requests.post(API_URL, headers=headers, json=payload)
        data = r.json()

        if not data.get("success"):
            print("Error:", data)
            break

        results = data.get("result", {}).get("data", [])
        all_logs.extend(results)

        cursor = data.get("result", {}).get("meta", {}).get("nextCursor")
        if not cursor:
            break

    return all_logs

def start_query(since, until):
    payload = {
        "queryId": QUERY_ID,
        "timeframe": {
            "from": since,
            "to": until
        }
    }

    r = requests.post(API_URL, headers=headers, json=payload)
    data = r.json()

    return data["result"]["run"]["id"]

def fetch_run(run_id):

    url = f"{API_URL}/run/{run_id}"

    r = requests.get(url, headers=headers)
    data = r.json()

    return data["result"]["data"]

if __name__ == "__main__":
    logs = fetch_logs()

    print("Fetched:", len(logs))

    with open("worker_logs.json", "w") as f:
        json.dump(logs, f, indent=2)
