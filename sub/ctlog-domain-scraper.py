import base64
import struct
import time
import requests

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID, NameOID


LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 200
TOTAL = 5000   # 每个 log 扫多少条（避免爆量）

OUTPUT_FILE = "domains.txt"


# ---------------------------
# 获取所有 CT logs
# ---------------------------
def get_ct_logs():
    r = requests.get(LOG_LIST_URL, timeout=30)
    r.raise_for_status()

    data = r.json()

    logs = []

    for operator in data["operators"]:
        for log in operator["logs"]:
            if log.get("state", {}).get("usable", {}).get("state") == "usable":
                logs.append({
                    "name": log["description"],
                    "url": log["url"]
                })

    return logs


# ---------------------------
# CT API
# ---------------------------
def get_tree_size(log_url):
    r = requests.get(f"{log_url}/ct/v1/get-sth", timeout=30)
    r.raise_for_status()
    return r.json()["tree_size"]


def fetch_entries(log_url, start, end):
    for _ in range(3):
        try:
            r = requests.get(
                f"{log_url}/ct/v1/get-entries?start={start}&end={end}",
                timeout=60
            )
            r.raise_for_status()
            return r.json()["entries"]
        except Exception as e:
            print(f"[retry] {log_url}: {e}")
            time.sleep(2)
    return []


# ---------------------------
# CT parser
# ---------------------------
def extract_cert(leaf_input_b64):
    data = base64.b64decode(leaf_input_b64)

    try:
        cert_len = struct.unpack(">I", b"\x00" + data[12:15])[0]
        cert = data[15:15 + cert_len]
        return cert
    except:
        return None


def extract_domains(cert):
    domains = set()

    try:
        c = x509.load_der_x509_certificate(cert, default_backend())

        # CN
        try:
            cn = c.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            domains.add(cn)
        except:
            pass

        # SAN
        try:
            san = c.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
            for d in san.value.get_values_for_type(x509.DNSName):
                domains.add(d)
        except:
            pass

    except:
        pass

    return domains


# ---------------------------
# main
# ---------------------------
def main():

    print("[+] loading CT log list...")

    logs = get_ct_logs()

    print(f"[+] usable logs: {len(logs)}")

    seen = set()
    written = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:

        for log in logs:

            log_url = log["url"]

            print(f"\n[+] scanning log: {log['name']}")
            print(f"[+] url: {log_url}")

            try:
                tree_size = get_tree_size(log_url)
            except:
                continue

            start_index = max(0, tree_size - TOTAL)

            print(f"[+] tree_size: {tree_size}")

            for start in range(start_index, tree_size, BATCH_SIZE):

                end = min(start + BATCH_SIZE - 1, tree_size - 1)

                entries = fetch_entries(log_url, start, end)

                for entry in entries:

                    cert = extract_cert(entry["leaf_input"])
                    if not cert:
                        continue

                    domains = extract_domains(cert)

                    for d in domains:
                        d = d.lower().strip()

                        if not d or d in seen:
                            continue

                        seen.add(d)

                        f.write(d + "\n")
                        written += 1

                f.flush()

            print(f"[+] current total domains: {written}")

    print("\n[+] DONE")
    print(f"[+] unique domains: {written}")
    print(f"[+] saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
