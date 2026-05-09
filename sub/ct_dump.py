import time
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import threading

LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 200
TOTAL = 1000

OUTPUT_DIR = "ct_dump_1000_debug"

# ---------------------------
# dynamic workers
# ---------------------------
CPU_BASE = os.cpu_count() or 4
MAX_WORKERS_LIMIT = CPU_BASE * 2   # 防止过载


# ---------------------------
# thread-local session (关键优化)
# ---------------------------
thread_local = threading.local()

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
    return thread_local.session


# ---------------------------
# load logs
# ---------------------------
def get_ct_logs():
    s = get_session()
    r = s.get(LOG_LIST_URL, timeout=30)
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
def get_tree_size(url):
    s = get_session()
    r = s.get(f"{url}/ct/v1/get-sth", timeout=30)
    r.raise_for_status()
    return r.json()["tree_size"]


# ---------------------------
def fetch_entries(url, start, end):
    s = get_session()

    for _ in range(2):
        try:
            r = s.get(
                f"{url}/ct/v1/get-entries?start={start}&end={end}",
                timeout=60
            )
            r.raise_for_status()
            return r.json().get("entries", [])
        except:
            time.sleep(1)

    return []


# ---------------------------
def safe_name(name):
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name)


# ---------------------------
def process_log(log):

    session = get_session()

    name = safe_name(log["name"])
    url = log["url"]

    print(f"[+] start {name}")

    try:
        tree_size = get_tree_size(url)
    except Exception as e:
        print(f"[!] fail {name}: {e}")
        return

    start_index = max(0, tree_size - TOTAL)

    all_entries = []

    for start in range(start_index, tree_size, BATCH_SIZE):
        end = min(start + BATCH_SIZE - 1, tree_size - 1)

        entries = fetch_entries(url, start, end)

        if entries:
            all_entries.extend(entries)

        time.sleep(0.05)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    out_path = os.path.join(OUTPUT_DIR, f"{name}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "log_name": log["name"],
            "log_url": url,
            "tree_size": tree_size,
            "entries_count": len(all_entries),
            "entries": all_entries
        }, f, ensure_ascii=False, indent=2)

    print(f"[+] done {name} -> {len(all_entries)} entries")


# ---------------------------
def main():

    print("[+] loading logs...")
    logs = get_ct_logs()

    log_count = len(logs)

    # ⭐ 核心：动态并发
    workers = min(log_count, MAX_WORKERS_LIMIT)

    print(f"[+] logs: {log_count}")
    print(f"[+] workers: {workers}")

    with ThreadPoolExecutor(max_workers=workers) as executor:

        futures = [executor.submit(process_log, log) for log in logs]

        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as e:
                print(f"[!] worker error: {e}")

    print("\n[+] DONE")
    print(f"[+] saved to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
