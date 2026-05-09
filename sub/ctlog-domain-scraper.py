import base64
import struct
import time
import requests

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtensionOID


LOG_URL = "https://ct.googleapis.com/logs/argon2026h1"

# 抓取最近多少条 CT entries
TOTAL = 10000

# 每批请求数量
BATCH_SIZE = 1000

# 输出文件
OUTPUT_FILE = "domains.txt"


def get_tree_size():
    r = requests.get(
        f"{LOG_URL}/ct/v1/get-sth",
        timeout=30
    )

    r.raise_for_status()

    return r.json()["tree_size"]


def fetch_entries(start, end):
    r = requests.get(
        f"{LOG_URL}/ct/v1/get-entries?start={start}&end={end}",
        timeout=60
    )

    r.raise_for_status()

    return r.json()["entries"]


def extract_cert(leaf_input_b64):
    """
    从 CT leaf_input 提取 DER 证书
    这里只处理 X509Entry
    """

    data = base64.b64decode(leaf_input_b64)

    # RFC6962:
    # data[12:15] 是证书长度
    cert_len = struct.unpack(
        ">I",
        b"\x00" + data[12:15]
    )[0]

    cert_der = data[15:15 + cert_len]

    return cert_der


def extract_domains(cert):
    domains = set()

    # Common Name
    try:
        cn = cert.subject.get_attributes_for_oid(
            x509.NameOID.COMMON_NAME
        )[0].value

        domains.add(cn)

    except Exception:
        pass

    # Subject Alternative Name
    try:
        san = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )

        for name in san.value.get_values_for_type(
            x509.DNSName
        ):
            domains.add(name)

    except Exception:
        pass

    return domains


def main():

    print("[+] getting tree size...")

    tree_size = get_tree_size()

    print(f"[+] current tree_size: {tree_size}")

    start_index = max(
        0,
        tree_size - TOTAL
    )

    print(
        f"[+] fetching entries "
        f"{start_index} - {tree_size - 1}"
    )

    # 已写入去重
    seen = set()

    total_written = 0
    total_failed = 0

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:

        for batch_start in range(
            start_index,
            tree_size,
            BATCH_SIZE
        ):

            batch_end = min(
                batch_start + BATCH_SIZE - 1,
                tree_size - 1
            )

            print(
                f"[+] batch "
                f"{batch_start} - {batch_end}"
            )

            try:
                entries = fetch_entries(
                    batch_start,
                    batch_end
                )

            except Exception as e:
                print(f"[!] fetch failed: {e}")
                continue

            for entry in entries:

                try:
                    cert_der = extract_cert(
                        entry["leaf_input"]
                    )

                    cert = x509.load_der_x509_certificate(
                        cert_der,
                        default_backend()
                    )

                    domains = extract_domains(cert)

                    for d in domains:

                        d = d.lower().strip()

                        if not d:
                            continue

                        # 去重
                        if d in seen:
                            continue

                        seen.add(d)

                        f.write(d + "\n")

                        total_written += 1

                except Exception as e:

                    total_failed += 1

                    continue

            # 立即写入磁盘
            f.flush()

            print(
                f"[+] written={total_written} "
                f"failed={total_failed}"
            )

            # 避免请求太快
            time.sleep(0.5)

    print("\n[+] done")
    print(f"[+] unique domains: {total_written}")
    print(f"[+] failed entries: {total_failed}")
    print(f"[+] saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
