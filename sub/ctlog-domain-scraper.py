import base64
import struct
import time
import requests

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID


LOG_URL = "https://ct.googleapis.com/logs/us1/argon2026h1"

BATCH_SIZE = 200
TOTAL = 10000

OUTPUT_FILE = "domains.txt"


# ---------------------------
# CT API
# ---------------------------
def get_tree_size():
    r = requests.get(
        f"{LOG_URL}/ct/v1/get-sth",
        timeout=30
    )
    r.raise_for_status()
    return r.json()["tree_size"]


def fetch_entries(start, end):
    for _ in range(3):
        try:
            r = requests.get(
                f"{LOG_URL}/ct/v1/get-entries?start={start}&end={end}",
                timeout=60
            )
            r.raise_for_status()
            return r.json()["entries"]
        except Exception as e:
            print(f"[retry] {e}")
            time.sleep(2)

    return []


# ---------------------------
# CT leaf parser (关键升级)
# ---------------------------
def extract_cert_from_leaf(leaf_input_b64):
    data = base64.b64decode(leaf_input_b64)

    try:
        # RFC6962:
        # 0: version
        # 1: leaf_type
        # 2-4: timestamp
        # 5: entry_type (关键！)

        entry_type = data[5]

        # X509Entry = 0
        # PrecertEntry = 1

        # certificate length offset differs slightly in practice
        # but CT format keeps cert after header

        if len(data) < 15:
            return None

        cert_len = struct.unpack(">I", b"\x00" + data[12:15])[0]
        cert = data[15:15 + cert_len]

        return cert

    except:
        return None


# ---------------------------
# domain extractor
# ---------------------------
def extract_domains(cert):
    domains = set()

    try:
        x509_cert = x509.load_der_x509_certificate(
            cert,
            default_backend()
        )

        # CN
        try:
            cn = x509_cert.subject.get_attributes_for_oid(
                x509.NameOID.COMMON_NAME
            )[0].value
            domains.add(cn)
        except:
            pass

        # SAN
        try:
            san = x509_cert.extensions.get_extension_for_oid(
                ExtensionOID.SUBJECT_ALTERNATIVE_NAME
            )

            for d in san.value.get_values_for_type(x509.DNSName):
                domains.add(d)

        except:
            pass

    except:
        return set()

    return domains


# ---------------------------
# main
# ---------------------------
def main():

    print("[+] getting tree size...")

    tree_size = get_tree_size()

    print(f"[+] tree_size: {tree_size}")

    start_index = max(0, tree_size - TOTAL)

    print(f"[+] fetching last {TOTAL} entries")

    seen = set()

    written = 0
    failed = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:

        for start in range(start_index, tree_size, BATCH_SIZE):

            end = min(start + BATCH_SIZE - 1, tree_size - 1)

            print(f"[+] batch {start} - {end}")

            entries = fetch_entries(start, end)

            for entry in entries:

                try:
                    cert = extract_cert_from_leaf(entry["leaf_input"])

                    if not cert:
                        failed += 1
                        continue

                    domains = extract_domains(cert)

                    for d in domains:
                        d = d.lower().strip()

                        if not d:
                            continue

                        if d in seen:
                            continue

                        seen.add(d)

                        f.write(d + "\n")
                        written += 1

                except:
                    failed += 1
                    continue

            f.flush()

            print(f"[+] written={written} failed={failed}")

            time.sleep(0.3)

    print("\n[+] done")
    print(f"[+] unique domains: {written}")
    print(f"[+] failed entries: {failed}")


if __name__ == "__main__":
    main()
