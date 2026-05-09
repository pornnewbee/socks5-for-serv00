import base64
import time
import hashlib
import requests

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID, NameOID


LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 200
TOTAL = 50000   # 每个Log的条数
OUTPUT_FILE = "domains.txt"

session = requests.Session()


# ---------------------------
# 获取 usable CT logs
# ---------------------------
def get_ct_logs():
    r = session.get(LOG_LIST_URL, timeout=30)
    r.raise_for_status()
    data = r.json()

    logs = []

    for operator in data["operators"]:
        for log in operator["logs"]:

            state = log.get("state", {})

            # usable
            if "usable" in state:
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
    try:
        r = session.get(
            f"{log_url}/ct/v1/get-entries?start={start}&end={end}",
            timeout=60
        )
        r.raise_for_status()
        return r.json().get("entries", [])
    except Exception as e:
        print(f"[error] fetch_entries {log_url}: {e}")
        return []


# ---------------------------
# leaf -> cert
# ---------------------------
def extract_cert(leaf_input_b64):
    try:
        data = base64.b64decode(leaf_input_b64)

        # CT MerkleTreeLeaf format:
        # [0] version
        # [1] leaf_type
        # ...
        # ASN.1 Cert starts at fixed offset
        # (标准 CT RFC6962)
        if len(data) < 10:
            return None

        # skip header (RFC6962 compatible)
        cert_start = 5

        cert_len = int.from_bytes(data[cert_start:cert_start + 3], "big")
        cert = data[cert_start + 3:cert_start + 3 + cert_len]

        return cert
    except:
        return None


# ---------------------------
# extract domains
# ---------------------------
def extract_domains(cert):
    domains = set()

    try:
        c = x509.load_der_x509_certificate(cert, default_backend())

        # CN
        try:
            cn = c.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            domains.add(cn.lower())
        except:
            pass

        # SAN
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
# main
# ---------------------------
def main():

    print("[+] loading CT logs...")

    logs = get_ct_logs()

    print(f"[+] usable logs: {len(logs)}")

    seen_certs = set()
    seen_domains = set()

    written = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:

        for log in logs:

            log_url = log["url"]

            print(f"\n[+] log: {log['name']}")

            try:
                tree_size = get_tree_size(log_url)
            except:
                continue

            start_index = max(0, tree_size - TOTAL)

            print(f"[+] size: {tree_size}")

            for start in range(start_index, tree_size, BATCH_SIZE):

                end = min(start + BATCH_SIZE - 1, tree_size - 1)

                entries = fetch_entries(log_url, start, end)

                for entry in entries:

                    cert = extract_cert(entry["leaf_input"])
                    if not cert:
                        continue

                    cert_hash = hashlib.sha256(cert).hexdigest()

                    if cert_hash in seen_certs:
                        continue

                    seen_certs.add(cert_hash)

                    domains = extract_domains(cert)

                    for d in domains:
                        d = d.strip().lower()

                        if not d:
                            continue

                        if d in seen_domains:
                            continue

                        seen_domains.add(d)

                        f.write(d + "\n")
                        written += 1

                # 防CT限流（很重要）
                time.sleep(0.2)
                f.flush()

            print(f"[+] domains: {written}")

    print("\n[+] DONE")
    print(f"[+] certs: {len(seen_certs)}")
    print(f"[+] domains: {written}")


if __name__ == "__main__":
    main()
