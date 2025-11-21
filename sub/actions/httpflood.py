import asyncio
import aiohttp

URL = "https://icy-river-2da8.alt-00e.workers.dev/"

# 统计用变量
total_requests = 0
status_counts = {}

lock = asyncio.Lock()

async def worker(session, worker_id):
    global total_requests

    while True:
        try:
            async with session.get(URL) as resp:
                code = resp.status

                async with lock:
                    total_requests += 1
                    status_counts[code] = status_counts.get(code, 0) + 1

        except Exception:
            # 统计异常情况
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

            # 重置计数器
            total_requests = 0
            status_counts = {}
            print("============================\n")


async def main():
    async with aiohttp.ClientSession() as session:
        workers = [asyncio.create_task(worker(session, i)) for i in range(99999)]
        stats_task = asyncio.create_task(stats_printer())
        await asyncio.gather(*workers, stats_task)


asyncio.run(main())
