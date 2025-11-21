import asyncio
import aiohttp
import time

URL = "https://icy-river-2da8.alt-00e.workers.dev/"

# 用于统计请求次数和HTTP状态码
total_requests = 0
status_codes = {
    200: 0,
    429: 0,
    500: 0,
    502: 0,
    503: 0,
    504: 0,
    # 你可以添加其他状态码的计数
}

lock = asyncio.Lock()  # 用于保护共享的统计数据

async def worker(session, worker_id):
    global total_requests
    while True:
        try:
            async with session.get(URL) as resp:
                # 获取HTTP状态码
                status_code = resp.status
                
                async with lock:
                    total_requests += 1
                    if status_code in status_codes:
                        status_codes[status_code] += 1
                    else:
                        status_codes[status_code] = 1  # 如果新状态码出现，初始化计数

                print(f"[worker {worker_id}] {status_code}")
        except Exception as e:
            print(f"[worker {worker_id}] error: {e}")

async def stats_printer():
    while True:
        await asyncio.sleep(10)  # 每 10 秒打印一次统计
        async with lock:
            print(f"\n--- Stats for the last 10 seconds ---")
            print(f"Total requests made: {total_requests}")
            for code, count in status_codes.items():
                print(f"HTTP {code}: {count}")
            total_requests = 0  # 重置计数器
            for code in status_codes:
                status_codes[code] = 0  # 重置每个状态码的计数

async def main():
    async with aiohttp.ClientSession() as session:
        # 启动 worker
        workers = [asyncio.create_task(worker(session, w)) for w in range(99999)]
        # 启动统计任务
        stats_task = asyncio.create_task(stats_printer())
        # 等待所有任务完成
        await asyncio.gather(*workers, stats_task)

# 执行
asyncio.run(main())
