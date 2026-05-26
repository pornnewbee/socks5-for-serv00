#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import aiohttp
import base64
import idna
import ssl
import time
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID, ExtensionOID


CT_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 512
MAX_ENTRIES_PER_LOG = 10000

CONCURRENCY_LOGS = 5
CONCURRENCY_FETCH = 10


# ================= STATS =================

stats = {
    "logs": 0,
    "entries": 0,
    "certs": 0,
    "failed": 0,
    "domains": 0
}


# ================= STORAGE =================

seen = set()

normal_file = open("normal_domains.txt", "w", encoding="utf-8")
wildcard_file = open("wildcard_domains.txt", "w", encoding="utf-8")
failed_file = open("failed_entries.log", "w", encoding="utf-8")


# ================= UTILS =================

def b64d(data: str) -> bytes:
    return base64.b64decode(data + "===")


def normalize_domain(d: str) -> str:
    d = d.strip().lower()

    if not d or "." not in d:
        return ""

    if d.replace(".", "").isdigit():
        return ""

    blacklist = (".local", ".localhost", ".internal")
    if any(d.endswith(x) for x in blacklist):
        return ""

    if d.startswith("*."):
        d = d[2:]

    try:
        d = idna.decode(d)
    except:
        pass

    return d


def save(domain: str):
    raw = domain.strip().lower()

    if not raw or "." not in raw:
        return

    if raw.replace(".", "").isdigit():
        return

    is_wildcard = raw.startswith("*.")

    clean = raw[2:] if is_wildcard else raw

    try:
        clean = idna.decode(clean)
    except:
        pass

    if clean in seen:
        return

    seen.add(clean)
    stats["domains"] += 1

    if is_wildcard:
        wildcard_file.write(clean + "\n")
    else:
        normal_file.write(clean + "\n")


# ================= CERT PARSER =================

def extract_domains(cert: x509.Certificate):
    out = set()

    try:
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        for x in cn:
            out.add(x.value)
    except:
        pass

    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        for dns in san.value.get_values_for_type(x509.DNSName):
            out.add(dns)
    except:
        pass

    return out


def parse_entry(entry: dict):
    stats["entries"] += 1

    leaf_b64 = entry.get("leaf_input")
    extra_b64 = entry.get("extra_data")

    if not leaf_b64:
        stats["failed"] += 1
        return None

    try:
        raw = b64d(leaf_b64)

        pos = 0
        pos += 1
        pos += 1
        pos += 8

        entry_type = int.from_bytes(raw[pos:pos+2], "big")
        pos += 2

        # X509Entry
        if entry_type == 0:
            cert_len = int.from_bytes(raw[pos:pos+3], "big")
            pos += 3

            cert = x509.load_der_x509_certificate(
                raw[pos:pos+cert_len],
                default_backend()
            )

            stats["certs"] += 1
            return cert

        # PrecertEntry
        if entry_type == 1 and extra_b64:
            extra = b64d(extra_b64)
            p = 0

            cert_len = int.from_bytes(extra[p:p+3], "big")
            p += 3

            cert = x509.load_der_x509_certificate(
                extra[p:p+cert_len],
                default_backend()
            )

            stats["certs"] += 1
            return cert

    except Exception:
        stats["failed"] += 1
        try:
            failed_file.write(leaf_b64 + "\n")
        except:
            pass
        return None

    return None


# ================= HTTP =================

async def fetch_json(session, url, retries=5):
    for i in range(retries):
        try:
            async with session.get(url, timeout=30) as r:

                # 成功
                if r.status == 200:
                    return await r.json()

                # 429: 线性退避
                if r.status == 429:
                    wait_time = (i + 1) * 2

                    print(
                        f"[429] {url} "
                        f"-> retry in {wait_time}s"
                    )

                    await asyncio.sleep(wait_time)
                    continue

                # 其它 HTTP 错误
                print(
                    f"[HTTP {r.status}] {url} "
                    f"-> retry {i+1}/{retries}"
                )

                await asyncio.sleep(1)

        except Exception as e:
            print(
                f"[EXCEPTION] {url} "
                f"-> {e} -> retry {i+1}/{retries}"
            )

            await asyncio.sleep(1)

    print(f"[FAILED] {url}")
    return None


async def fetch_entries(session, log_url, start, end, retries=5):
    url = f"{log_url}/ct/v1/get-entries?start={start}&end={end}"

    for i in range(retries):
        try:
            async with session.get(url, timeout=60) as r:

                if r.status == 200:
                    data = await r.json()
                    return data.get("entries", [])

                # 429: 线性退避
                if r.status == 429:
                    wait_time = (i + 1) * 2
                    print(f"[429] entries {start}-{end} -> retry in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue

                # 其它错误
                print(
                    f"[HTTP {r.status}] entries {start}-{end} "
                    f"-> retry {i+1}/{retries}"
                )

                await asyncio.sleep(1)

        except Exception as e:
            print(
                f"[EXCEPTION] entries {start}-{end} "
                f"-> {e} -> retry {i+1}/{retries}"
            )

            await asyncio.sleep(1)

    print(f"[FAILED] entries {start}-{end}")
    return []


# ================= LOG WORKER =================

async def process_log(session, sem, log):
    url = log.get("url")
    desc = log.get("description", "unknown")

    if not url:
        return

    print(f"[+] log: {desc}")
    stats["logs"] += 1

    sth = await fetch_json(session, f"{url}/ct/v1/get-sth")
    if not sth:
        return

    tree_size = sth.get("tree_size", 0)
    if tree_size == 0:
        return

    start_index = max(0, tree_size - MAX_ENTRIES_PER_LOG)

    tasks = []

    async with sem:
        for start in range(start_index, tree_size, BATCH_SIZE):
            end = min(start + BATCH_SIZE - 1, tree_size - 1)
            tasks.append(fetch_entries(session, url, start, end))

        results = await asyncio.gather(*tasks)

    for entries in results:
        if not entries:
            continue

        for e in entries:
            cert = parse_entry(e)
            if not cert:
                continue

            for d in extract_domains(cert):
                save(d)

    print(f"    entries: {stats['entries']}")


# ================= MAIN =================

async def main():
    start_time = time.time()

    ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(limit=CONCURRENCY_FETCH, ssl=ssl_ctx)

    async with aiohttp.ClientSession(connector=connector) as session:

        data = await fetch_json(session, CT_LOG_LIST_URL)

        logs = []
        for op in data.get("operators", []):
            logs.extend(op.get("logs", []))

        print(f"[+] logs: {len(logs)}")

        sem = asyncio.Semaphore(CONCURRENCY_LOGS)

        await asyncio.gather(*[
            process_log(session, sem, log)
            for log in logs
        ])

    normal_file.close()
    wildcard_file.close()
    failed_file.close()

    duration = time.time() - start_time

    print("\n========== SUMMARY ==========")
    print(f"Logs processed     : {stats['logs']}")
    print(f"Entries scanned    : {stats['entries']}")
    print(f"Certificates       : {stats['certs']}")
    print(f"Failed parses      : {stats['failed']}")
    print(f"Unique domains     : {stats['domains']}")
    print(f"Runtime            : {duration:.2f}s")
    print("=============================\n")

    print("[+] output:")
    print("  normal_domains.txt")
    print("  wildcard_domains.txt")
    print("  failed_entries.log")


if __name__ == "__main__":
    asyncio.run(main())
