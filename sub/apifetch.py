import os
import requests
from datetime import datetime, timedelta, timezone
import json
import time

CF_API_TOKEN = os.environ["CF_API_TOKEN"]
CF_ACCOUNT_ID = os.environ["API_ACCOUNT_ID"]

QUERY_ID = "gbax5izkb3b4b1y4ne9hgrja"

BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/workers/observability/telemetry/query"

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

def dry_run(since, until):
    payload = {
        "queryId": QUERY_ID,
        "dry": True,
        "timeframe": {"from": since, "to": until}
    }

    r = requests.post(BASE_URL, headers=headers, json=payload)
    print("Dry:", r.text)

def start_query(since, until):
    payload = {
        "queryId": QUERY_ID,
        "timeframe": {"from": since, "to": until}
    }

    r = requests.post(BASE_URL, headers=headers, json=payload)
    data = r.json()

    return data["result"]["run"]["id"]

def wait_run_complete(run_id):
    url = f"{BASE_URL}/run/{run_id}"

    while True:
        r = requests.get(url, headers=headers)
        data = r.json()

        status = data["result"]["run"]["status"]

        if status == "COMPLETED":
            return
        if status == "FAILED":
            raise Exception("Query run failed")

        time.sleep(2)

def fetch_run_data(run_id):
    url = f"{BASE_URL}/run/{run_id}"

    r = requests.get(url, headers=headers)
    data = r.json()

    return data["result"]["data"]

if __name__ == "__main__":

    since, until = get_utc_timeframe()

    dry_run(since, until)

    run_id = start_query(since, until)

    print("Run ID:", run_id)

    wait_run_complete(run_id)

    logs = fetch_run_data(run_id)

    print("Fetched:", len(logs))

    with open("worker_logs.json", "w") as f:
        json.dump(logs, f, indent=2)
