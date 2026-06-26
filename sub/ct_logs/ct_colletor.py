#!/usr/bin/env python3
# -*- coding: utf-8 -*- 

import asyncio
import aiohttp
import base64
import idna
import ssl
import time
from collections import Counter  # 新增：用于内存计数
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID, ExtensionOID


CT_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 512
MAX_ENTRIES_PER_LOG = 1000
HTTP_RETRIES = 10      # 普通错误重试次数
RATE_RETRIES = 10      # 429重试次数
CONCURRENCY_LOGS = 5
CONCURRENCY_FETCH = 10

# ================= NOISE FILTER CONFIG =================
NOISE_THRESHOLD = 250        # 阈值设置为 250
level1_counter = Counter()    # 用于统计一级域名出现次数
muted_suffixes = set()       # 用于记录已经打印过警告的噪音域名，防止控制台刷屏


# ================= STATS =================

stats = {
    "logs": 0,
    "entries": 0,
    "certs": 0,
    "failed": 0,
    "domains": 0,
    "noise_dropped": 0       # 新增：统计因噪音丢弃的域名数
}


# ================= STORAGE =================

seen = set()

normal_file = open("normal_domains.txt", "w", encoding="utf-8")
# 删除了 wildcard_file，因为泛域名直接丢弃
failed_file = open("failed_entries.log", "w", encoding="utf-8")
failed_batches_file = open(
    "failed_batches.log",
    "w",
    encoding="utf-8"  # 加上 encoding= 即可解决
)

# ================= UTILS =================

def b64d(data: str) -> bytes:
    return base64.b64decode(data + "===")

def sort_domain_file(path):

    with open(path, "r", encoding="utf-8") as f:
        domains = [
            line.strip()
            for line in f
            if line.strip()
        ]

    domains.sort(
        key=lambda d: tuple(
            reversed(d.lower().split("."))
        )
    )

    with open(path, "w", encoding="utf-8") as f:
        for d in domains:
            f.write(d + "\n")

def get_registered_domain(domain: str) -> str:
    """简易获取一级注册域名 (例如 sub.example.com -> example.com)"""
    parts = domain.split(".")
    if len(parts) >= 2:
        # 取最后两段，如 example.com (应对大多数常规域名)
        # 注意：此处未引入 tldextract，如遇到 .com.cn 等复合后缀可根据需要换成 tldextract
        return ".".join(parts[-2:])
    return domain


def save(domain: str):
    raw = domain.strip().lower()

    if not raw or "." not in raw:
        return

    if raw.replace(".", "").isdigit():
        return

    # ---------------- 环节 1: 泛域名不要 ----------------
    if raw.startswith("*."):
        return  # 显式丢弃泛域名，不再保存

    try:
        clean = idna.decode(raw)
    except:
        clean = raw

    if clean in seen:
        return

    # ---------------- 环节 2: 噪音频次过滤 ----------------
    reg_domain = get_registered_domain(clean)
    
    # 累加该一级域名的计数
    level1_counter[reg_domain] += 1

    if level1_counter[reg_domain] > NOISE_THRESHOLD:
        stats["noise_dropped"] += 1
        if reg_domain not in muted_suffixes:
            muted_suffixes.add(reg_domain)
            print(f"[!] 发现高频噪音源，已拦截后续子域: *.{reg_domain} (超过 {NOISE_THRESHOLD} 次)")
        return  # 超过阈值，直接拦截，不投入后续环节

    # ---------------- 环节 3: 正常留存 ----------------
    seen.add(clean)
    stats["domains"] += 1
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

async def fetch_json(session, url):

    http_retry = 0
    rate_retry = 0

    while True:
        try:
            async with session.get(url, timeout=30) as r:

                if r.status == 200:
                    return await r.json()

                if r.status == 429:

                    rate_retry += 1

                    if rate_retry > RATE_RETRIES:

                        failed_batches_file.write(
                            f"JSON,{url},429\n"
                        )

                        print(f"[FAILED 429] {url}")
                        return None

                    wait_time = rate_retry * 2

                    print(
                        f"[429] {url} "
                        f"retry {rate_retry}/{RATE_RETRIES} "
                        f"in {wait_time}s"
                    )

                    await asyncio.sleep(wait_time)
                    continue

                http_retry += 1

                if http_retry > HTTP_RETRIES:

                    failed_batches_file.write(
                        f"JSON,{url},HTTP-{r.status}\n"
                    )

                    print(f"[FAILED HTTP] {url}")
                    return None

                print(
                    f"[HTTP {r.status}] {url} "
                    f"retry {http_retry}/{HTTP_RETRIES}"
                )

                await asyncio.sleep(1)

        except Exception as e:

            http_retry += 1

            if http_retry > HTTP_RETRIES:

                failed_batches_file.write(
                    f"JSON,{url},EXCEPTION\n"
                )

                print(f"[FAILED EXCEPTION] {url}")
                return None

            print(
                f"[EXCEPTION] {url} "
                f"-> {e} "
                f"retry {http_retry}/{HTTP_RETRIES}"
            )

            await asyncio.sleep(1)


async def fetch_entries(session, log_url, start, end):

    url = (
        f"{log_url}/ct/v1/get-entries"
        f"?start={start}&end={end}"
    )

    http_retry = 0
    rate_retry = 0

    while True:
        try:
            async with session.get(url, timeout=60) as r:

                if r.status == 200:

                    data = await r.json()
                    return data.get("entries", [])

                if r.status == 429:

                    rate_retry += 1

                    if rate_retry > RATE_RETRIES:

                        failed_batches_file.write(
                            f"{log_url},{start},{end},429\n"
                        )

                        print(
                            f"[FAILED 429] "
                            f"{start}-{end}"
                        )

                        return []

                    wait_time = rate_retry * 2

                    print(
                        f"[429] entries {start}-{end} "
                        f"retry {rate_retry}/{RATE_RETRIES} "
                        f"in {wait_time}s"
                    )

                    await asyncio.sleep(wait_time)
                    continue

                http_retry += 1

                if http_retry > HTTP_RETRIES:

                    failed_batches_file.write(
                        f"{log_url},{start},{end},HTTP-{r.status}\n"
                    )

                    print(
                        f"[FAILED HTTP] "
                        f"{start}-{end}"
                    )

                    return []

                print(
                    f"[HTTP {r.status}] "
                    f"entries {start}-{end} "
                    f"retry {http_retry}/{HTTP_RETRIES}"
                )

                await asyncio.sleep(1)

        except Exception as e:

            http_retry += 1

            if http_retry > HTTP_RETRIES:

                failed_batches_file.write(
                    f"{log_url},{start},{end},EXCEPTION\n"
                )

                print(
                    f"[FAILED EXCEPTION] "
                    f"{start}-{end}"
                )

                return []

            print(
                f"[EXCEPTION] entries {start}-{end} "
                f"-> {e} "
                f"retry {http_retry}/{HTTP_RETRIES}"
            )

            await asyncio.sleep(1)


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

        sem = asyncio.semaphore(CONCURRENCY_LOGS)

        await asyncio.gather(*[
            process_log(session, sem, log)
            for log in logs
        ])

    normal_file.close()
    failed_file.close()
    failed_batches_file.close()
    
    print("[+] sorting domains...")
    sort_domain_file("normal_domains.txt")

    duration = time.time() - start_time

    print("\n========== SUMMARY ==========")
    print(f"Logs processed     : {stats['logs']}")
    print(f"Entries scanned    : {stats['entries']}")
    print(f"Certificates       : {stats['certs']}")
    print(f"Failed parses      : {stats['failed']}")
    print(f"Noise hits dropped : {stats['noise_dropped']}") # 打印因高频被剔除的数量
    print(f"Unique normal domains: {stats['domains']}")
    print(f"Runtime            : {duration:.2f}s")
    print("=============================\n")

    print("[+] output:")
    print("  normal_domains.txt")
    print("  failed_entries.log")
    print("  failed_batches.log")

if __name__ == "__main__":
    asyncio.run(main())
