import asyncio
import aiohttp
import time

URL = "https://icy-river-2da8.alt-00e.workers.dev/"

# ====== 限速配置 ======
RATE_LIMIT = 500          # 每秒最大请求数
TOKENS = RATE_LIMIT       # 当前可用的 tokens
last_refill = time.time()

# ====== 统计数据 ======
total_requests = 0
status_counts = {}
lock = asyncio.Lock()

# ====== 令牌桶机制 ======
async def acquire_token():
    global TOKENS, last_refill

    while True:
        now = time.time()

        # 每秒补充一次 tokens
        if now - last_refill >= 1:
            TOKENS = RATE_LIMIT
            last_refill = now

        if TOKENS > 0:
            TOKENS -= 1
            return
        else:
            # 无 token，等 1ms 再尝试
            await asyncio.sleep(0.001)


async def worker(session):
    global total_requests, status_counts

    while True:
        await acquire_token()

        try:
            async with session.get(URL) as resp:
                code = resp.status

                async with lock:
                    total_requests += 1
                    status_counts[code] = status_counts.get(code, 0) + 1

        except Exception:
            async with lock:
                status_counts["error"] = status_counts.get("error", 0) + 1


async def stats_printer():
    global total_requests, status_counts

    while True:
        await asyncio.sleep(10)

        async with lock:
            print("\n====== 10s Statistics ======")
            print(f"Total requests: {total_requests}")

            for code, count in status_counts.items():
                print(f"HTTP {code}: {count}")

            total_requests = 0
            status_counts = {}
            print("============================\n")


async def main():
    async with aiohttp.ClientSession() as session:
        # 开很多 worker，但每秒最多 500 请求（由 token bucket 控制）
        workers = [asyncio.create_task(worker(session)) for _ in range(2000)]
        stats_task = asyncio.create_task(stats_printer())
        await asyncio.gather(*workers, stats_task)

asyncio.run(main())
