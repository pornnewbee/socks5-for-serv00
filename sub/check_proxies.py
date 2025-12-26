import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
import aiohttp
from aiohttp_socks import ProxyConnector

TEST_URL = "https://www.gstatic.com/generate_204"
TCP_TIMEOUT = 3
HTTP_TIMEOUT = 5
MAX_TCP_THREADS = 100   # TCP线程池大小
MAX_HTTP_CONCURRENT = 50  # HTTP异步并发量

# 读取代理文件
def load_proxies(file_path):
    proxies = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) != 3:
                continue
            ip, port, proto = parts
            proxies.append({"ip": ip, "port": int(port), "protocol": proto.lower()})
    return proxies

# TCP 检测（阻塞，用线程池）
def check_tcp_blocking(ip, port):
    try:
        sock = socket.create_connection((ip, port), TCP_TIMEOUT)
        sock.close()
        return True
    except:
        return False

# 异步封装线程池 TCP 检测
async def check_tcp(executor, ip, port):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, check_tcp_blocking, ip, port)

# 异步验证代理 HTTP 可用性
async def check_http(proxy, semaphore):
    ip = proxy["ip"]
    port = proxy["port"]
    proto = proxy["protocol"]

    connector = ProxyConnector.from_url(f"{proto}://{ip}:{port}")
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with semaphore:
        try:
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                async with session.get(TEST_URL) as resp:
                    if resp.status == 204:
                        print(f"[OK] {ip}:{port} ({proto})")
                        return True
                    else:
                        print(f"[HTTP FAIL] {ip}:{port} status={resp.status}")
        except Exception as e:
            print(f"[PROXY FAIL] {ip}:{port} {e}")
    return False

# 单个代理完整检测
async def check_proxy(executor, semaphore, proxy):
    tcp_ok = await check_tcp(executor, proxy["ip"], proxy["port"])
    if not tcp_ok:
        print(f"[TCP FAIL] {proxy['ip']}:{proxy['port']}")
        return False
    return await check_http(proxy, semaphore)

# 批量异步验证
async def main(file_path):
    proxies = load_proxies(file_path)
    executor = ThreadPoolExecutor(max_workers=MAX_TCP_THREADS)
    semaphore = asyncio.Semaphore(MAX_HTTP_CONCURRENT)

    tasks = [check_proxy(executor, semaphore, p) for p in proxies]
    results = await asyncio.gather(*tasks)

    valid_proxies = [p for p, ok in zip(proxies, results) if ok]
    print(f"\n✅ 有效代理 {len(valid_proxies)}/{len(proxies)}")
    for p in valid_proxies:
        print(f"{p['ip']}:{p['port']} ({p['protocol']})")

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} proxy_file.txt")
        exit(1)
    asyncio.run(main(sys.argv[1]))
