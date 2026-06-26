#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import aiohttp
import base64
import idna
import ssl
import time
from collections import Counter
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import NameOID, ExtensionOID


CT_LOG_LIST_URL = "https://www.gstatic.com/ct/log_list/v3/log_list.json"

BATCH_SIZE = 512
MAX_ENTRIES_PER_LOG = 1000
HTTP_RETRIES = 10
RATE_RETRIES = 10
CONCURRENCY_LOGS = 5
CONCURRENCY_FETCH = 10

# ================= NOISE FILTER CONFIG =================
NOISE_THRESHOLD = 250
candidate_domains = {}  # key: reg_domain, value: set of full domains
noise_domains = set()   # 确认噪音的一级域名，后续直接丢弃
muted_suffixes = set()

# ================= STATS =================
stats = {
    "logs": 0,
    "entries": 0,
    "certs": 0,
    "failed": 0,
    "domains": 0,
    "noise_dropped": 0
}

# ================= STORAGE (文件延迟创建) =================
failed_file = open("failed_entries.log", "w", encoding="utf-8")
failed_batches_file = open("failed_batches.log", "w", encoding="utf-8")


# ================= UTILS =================

def b64d(data: str) -> bytes:
    return base64.b64decode(data + "===")


def get_registered_domain(domain: str) -> str:
    parts = domain.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return domain


# ================= 新的域名处理逻辑 =================

def process_domain(domain: str):
    """边抓边计数，超过阈值后回溯清除并静默"""
    raw = domain.strip().lower()

    # 基本过滤
    if not raw or "." not in raw:
        return
    if raw.startswith("*."):          # 泛域名直接丢弃
        return
    if raw.replace(".", "").isdigit():
        return

    try:
        clean = idna.decode(raw)
    except:
        clean = raw

    reg = get_registered_domain(clean)

    # 如果该一级域名已被标记为噪音，直接丢弃
    if reg in noise_domains:
        stats["noise_dropped"] += 1
        return

    # 未标记噪音，加入候选
    if reg not in candidate_domains:
        candidate_domains[reg] = set()
    candidate_domains[reg].add(clean)

    # 检查是否首次超过阈值
    if len(candidate_domains[reg]) > NOISE_THRESHOLD:
        # 触发噪音清除：丢弃该 reg 下所有已收集域名
        removed_count = len(candidate_domains[reg])
        del candidate_domains[reg]
        noise_domains.add(reg)
        stats["noise_dropped"] += removed_count

        if reg not in muted_suffixes:
            muted_suffixes.add(reg)
            print(f"[!] 发现高频噪音源，已彻底清除 *.{reg} (超过 {NOISE_THRESHOLD} 次)")
        return

    # 未达阈值，正常累计（但暂不写文件）


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
        if entry_type == 0:
            cert_len = int.from_bytes(raw[pos:pos+3], "big")
            pos += 3
            cert = x509.load_der_x509_certificate(raw[pos:pos+cert_len], default_backend())
            stats["certs"] += 1
            return cert
        if entry_type == 1 and extra_b64:
            extra = b64d(extra_b64)
            p = 0
            cert_len = int.from_bytes(extra[p:p+3], "big")
            p += 3
            cert = x509.load_der_x509_certificate(extra[p:p+cert_len], default_backend())
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
                        failed_batches_file.write(f"JSON,{url},429\n")
                        print(f"[FAILED 429] {url}")
                        return None
                    wait_time = rate_retry * 2
                    print(f"[429] {url} retry {rate_retry}/{RATE_RETRIES} in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                http_retry += 1
                if http_retry > HTTP_RETRIES:
                    failed_batches_file.write(f"JSON,{url},HTTP-{r.status}\n")
                    print(f"[FAILED HTTP] {url}")
                    return None
                print(f"[HTTP {r.status}] {url} retry {http_retry}/{HTTP_RETRIES}")
                await asyncio.sleep(1)
        except Exception as e:
            http_retry += 1
            if http_retry > HTTP_RETRIES:
                failed_batches_file.write(f"JSON,{url},EXCEPTION\n")
                print(f"[FAILED EXCEPTION] {url}")
                return None
            print(f"[EXCEPTION] {url} -> {e} retry {http_retry}/{HTTP_RETRIES}")
            await asyncio.sleep(1)


async def fetch_entries(session, log_url, start, end):
    url = f"{log_url}/ct/v1/get-entries?start={start}&end={end}"
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
                        failed_batches_file.write(f"{log_url},{start},{end},429\n")
                        print(f"[FAILED 429] {start}-{end}")
                        return []
                    wait_time = rate_retry * 2
                    print(f"[429] entries {start}-{end} retry {rate_retry}/{RATE_RETRIES} in {wait_time}s")
                    await asyncio.sleep(wait_time)
                    continue
                http_retry += 1
                if http_retry > HTTP_RETRIES:
                    failed_batches_file.write(f"{log_url},{start},{end},HTTP-{r.status}\n")
                    print(f"[FAILED HTTP] {start}-{end}")
                    return []
                print(f"[HTTP {r.status}] entries {start}-{end} retry {http_retry}/{HTTP_RETRIES}")
                await asyncio.sleep(1)
        except Exception as e:
            http_retry += 1
            if http_retry > HTTP_RETRIES:
                failed_batches_file.write(f"{log_url},{start},{end},EXCEPTION\n")
                print(f"[FAILED EXCEPTION] {start}-{end}")
                return []
            print(f"[EXCEPTION] entries {start}-{end} -> {e} retry {http_retry}/{HTTP_RETRIES}")
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
                process_domain(d)   # 改用新的内存过滤函数


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
        await asyncio.gather(*[process_log(session, sem, log) for log in logs])

    # 所有日志处理完毕，合并所有候选域名
    all_clean = set()
    for dom_set in candidate_domains.values():
        all_clean.update(dom_set)

    # 写入文件（一次性写入，避免之前的碎片化 I/O）
    normal_file = open("normal_domains.txt", "w", encoding="utf-8")
    for d in sorted(all_clean, key=lambda x: tuple(reversed(x.split(".")))):
        normal_file.write(d + "\n")
    normal_file.close()

    stats["domains"] = len(all_clean)

    failed_file.close()
    failed_batches_file.close()

    duration = time.time() - start_time

    print("\n========== SUMMARY ==========")
    print(f"Logs processed     : {stats['logs']}")
    print(f"Entries scanned    : {stats['entries']}")
    print(f"Certificates       : {stats['certs']}")
    print(f"Failed parses      : {stats['failed']}")
    print(f"Noise hits dropped : {stats['noise_dropped']}")
    print(f"Unique normal domains: {stats['domains']}")
    print(f"Runtime            : {duration:.2f}s")
    print("=============================\n")

    print("[+] output:")
    print("  normal_domains.txt")
    print("  failed_entries.log")
    print("  failed_batches.log")

if __name__ == "__main__":
    asyncio.run(main())
