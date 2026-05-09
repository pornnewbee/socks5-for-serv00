import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import os

LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 200
TOTAL = 1000          # ⭐ 每个 log 只抓 1000 条
MAX_WORKERS = 6       # 并发 log 数

session = requests.Session()

OUTPUT_DIR = "ct_dump_1000_debug"


# ---------------------------
# load logs
# ---------------------------
def get_ct_logs():
    r = session.get(LOG_LIST_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    logs = []

    for op in data["operators"]:
        for log in op["logs"]:
            if "usable" in log.get("state", {}):
                logs.append({
                    "name": log["description"],
                    "url": log["url"]
                })

    return logs


# ---------------------------
# get size
# ---------------------------
def get_tree_size(log_url):
    r = session.get(f"{log_url}/ct/v1/get-sth", timeout=30)
    r.raise_for_status()
    return r.json()["tree_size"]


# ---------------------------
# fetch entries
# ---------------------------
def fetch_entries(log_url, start, end):
    for _ in range(2):
        try:
            r = session.get(
                f"{log_url}/ct/v1/get-entries?start={start}&end={end}",
                timeout=60
            )
            r.raise_for_status()
            return r.json().get("entries", [])
        except Exception:
            time.sleep(1)
    return []


# ---------------------------
# sanitize filename
# ---------------------------
def safe_name(name):
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name)


# ---------------------------
# worker
# ---------------------------
def process_log(log):

    name = safe_name(log["name"])
    url = log["url"]

    print(f"[+] start {name}")

    try:
        tree_size = get_tree_size(url)
    except Exception as e:
        print(f"[!] skip {name} get_tree_size failed: {e}")
        return

    start_index = max(0, tree_size - TOTAL)

    all_entries = []

    for start in range(start_index, tree_size, BATCH_SIZE):
        end = min(start + BATCH_SIZE - 1, tree_size - 1)

        entries = fetch_entries(url, start, end)

        if entries:
            all_entries.extend(entries)

        time.sleep(0.1)

    out_path = os.path.join(OUTPUT_DIR, f"{name}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "log_name": log["name"],
            "log_url": url,
            "tree_size": tree_size,
            "entries_count": len(all_entries),
            "entries": all_entries
        }, f, ensure_ascii=False, indent=2)

    print(f"[+] done {name} -> {len(all_entries)} entries saved")


# ---------------------------
# main
# ---------------------------
def main():

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("[+] loading logs...")
    logs = get_ct_logs()
    print(f"[+] usable logs: {len(logs)}")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(process_log, log) for log in logs]

        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"[!] worker error: {e}")

    print("\n[+] DONE")
    print(f"[+] saved to: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
