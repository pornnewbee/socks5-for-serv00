import base64
import time
import hashlib
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID, NameOID


LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 200
TOTAL = 50000
OUTPUT_FILE = "domains.txt"

MAX_WORKERS = 4   # ⭐ 并发log数量（建议3~6）

session = requests.Session()


# ---------------------------
# get logs
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
# CT API
# ---------------------------
def get_tree_size(log_url):
    r = session.get(f"{log_url}/ct/v1/get-sth", timeout=30)
    r.raise_for_status()
    return r.json()["tree_size"]


def fetch_entries(log_url, start, end):
    for _ in range(2):
        try:
            r = session.get(
                f"{log_url}/ct/v1/get-entries?start={start}&end={end}",
                timeout=60
            )
            r.raise_for_status()
            return r.json().get("entries", [])
        except:
            time.sleep(1)
    return []


# ---------------------------
# cert extract
# ---------------------------
def extract_cert(leaf_input_b64):
    try:
        data = base64.b64decode(leaf_input_b64)

        if len(data) < 10:
            return None

        cert_start = 5
        cert_len = int.from_bytes(data[cert_start:cert_start + 3], "big")

        cert = data[cert_start + 3:cert_start + 3 + cert_len]
        return cert

    except:
        return None


def extract_domains(cert):
    domains = set()

    try:
        c = x509.load_der_x509_certificate(cert, default_backend())

        try:
            cn = c.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            domains.add(cn.lower())
        except:
            pass

        try:
            san = c.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )

            for d in san.value.get_values_for_type(x509.DNSName):
                domains.add(d.lower())

        except:
            pass

    except:
        pass

    return domains


# ---------------------------
# worker per log
# ---------------------------
def process_log(log):

    log_url = log["url"]
    name = log["name"]

    print(f"[+] start {name}")

    local_certs = set()
    local_domains = set()
    results = []

    try:
        tree_size = get_tree_size(log_url)
    except:
        return []

    start_index = max(0, tree_size - TOTAL)

    for start in range(start_index, tree_size, BATCH_SIZE):

        end = min(start + BATCH_SIZE - 1, tree_size - 1)

        entries = fetch_entries(log_url, start, end)

        for entry in entries:

            cert = extract_cert(entry["leaf_input"])
            if not cert:
                continue

            h = hashlib.sha256(cert).hexdigest()

            if h in local_certs:
                continue

            local_certs.add(h)

            domains = extract_domains(cert)

            for d in domains:
                d = d.strip().lower()

                if not d:
                    continue

                if d in local_domains:
                    continue

                local_domains.add(d)
                results.append(d)

        time.sleep(0.1)

    print(f"[+] done {name} -> {len(results)} domains")
    return results


# ---------------------------
# main
# ---------------------------
def main():

    print("[+] loading logs...")
    logs = get_ct_logs()
    print(f"[+] usable logs: {len(logs)}")

    seen = set()
    total_written = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:

            futures = [executor.submit(process_log, log) for log in logs]

            for fut in as_completed(futures):

                try:
                    domains = fut.result()
                except:
                    continue

                for d in domains:
                    if d in seen:
                        continue

                    seen.add(d)
                    f.write(d + "\n")
                    total_written += 1

                f.flush()

    print("\n[+] DONE")
    print(f"[+] domains: {total_written}")


if __name__ == "__main__":
    main()
