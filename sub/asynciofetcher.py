import os
import sys
import json
import copy
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone

# ===================== 配置区 =====================
SEGMENTS_PER_DAY = 48                  # 每天拆成几段
MAX_RETRIES = 5                        # 单页请求最大重试次数 未使用
BACKOFF = 0                             # 重试基数秒，0 表示不限速
MAX_CONCURRENT_ACCOUNTS = 1             # 同时查询账户数
FOLLOWER_START_INTERVAL = 1             # 从线程启动间隔秒
FOLLOWER_RECOVERY_INTERVAL = 1          # 从线程恢复任务间隔秒
# ==================================================

# 从环境变量读取 ACCOUNTS
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
if not ACCOUNTS_JSON:
    print("❌ 未检测到环境变量 ACCOUNTS_JSON，请在 GitHub Secrets 设置")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
except json.JSONDecodeError:
    print("❌ ACCOUNTS_JSON 内容不是合法 JSON")
    sys.exit(1)

URL_TEMPLATE = "https://dash.cloudflare.com/api/v4/accounts/{account_id}/workers/observability/telemetry/query"
LOCAL_COOKIE = os.getenv("CF_COOKIE") or ""
if not LOCAL_COOKIE or len(LOCAL_COOKIE) < 20:
    print("❌ 未检测到有效 CF_COOKIE，请在环境变量 CF_COOKIE 中设置")
    sys.exit(1)

HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://dash.cloudflare.com",
    "referer": "https://dash.cloudflare.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "workers-observability-origin": "workers-logs",
    "x-cross-site-security": "dash",
    "cookie": LOCAL_COOKIE,
}


def get_date_list(arg: str):
    n = int(arg) if arg and arg.isdigit() else 1
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def split_timeframes(date_str, segments=SEGMENTS_PER_DAY):
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, 0, 0, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    step = (end_ms - start_ms) // segments
    ranges = []
    for i in range(segments):
        seg_start = start_ms + i * step
        seg_end = seg_start + step
        if i == segments - 1:
            seg_end = end_ms
        ranges.append((seg_start, seg_end))
    return ranges


async def fetch_segment(session, account_id, service_name, seg_id, start_ms, end_ms, paused_queue=None):
    """抓取单段日志（分页 + 无限重试 + 线性退避 + 安全解析）"""
    all_logs = {}
    offset = None
    page = 0

    base_data = {
        "view": "invocations",
        "queryId": "workers-logs-invocations",
        "limit": 100,
        "parameters": {
            "datasets": ["cloudflare-workers"],
            "filters": [
                {"key": "$metadata.service", "type": "string", "value": service_name, "operation": "eq"}
            ],
            "calculations": [],
            "groupBys": [],
            "havings": []
        },
        "timeframe": {"from": start_ms, "to": end_ms}
    }

    while True:
        data = copy.deepcopy(base_data)
        if offset:
            data["offset"] = offset

        attempt = 1
        while True:
            try:
                    async with session.post(URL_TEMPLATE.format(account_id=account_id),
                                            headers=HEADERS, json=data, timeout=15) as resp:
                        status = resp.status
                        text = await resp.text()
                        if status == 200:
                            result = await resp.json()
                            break  # 成功退出重试循环
                        else:
                            print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 HTTP {status}")
                            print(f"⚠️ 返回内容: {text[:300]}")

                            # 遇到 429 的从线程任务暂时挂起
                            if status == 429 and paused_queue is not None:
                                print(f"♻️ {account_id}/{service_name} 第{seg_id}段暂停从线程，等待恢复")
                                await paused_queue.put((seg_id, start_ms, end_ms))
                                return all_logs  # 暂停当前段，返回已抓取日志
            except Exception as e:
                print(f"❌ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 异常: {e}")

            # 无限重试 + 线性退避
            delay = min(0.5 * attempt, 10)
            print(f"⏳ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 第 {attempt} 次重试, 等待 {delay:.1f}s")
            await asyncio.sleep(delay)
            attempt += 1

        # JSON 安全解析
        try:
            if not result or "result" not in result or "invocations" not in result["result"]:
                print(f"❌ {account_id}/{service_name} 第{seg_id}段 空或异常响应")
                break
            invocations = result["result"].get("invocations", {})
        except Exception as e:
            print(f"❌ {account_id}/{service_name} 第{seg_id}段 JSON 解析异常: {e}")
            await asyncio.sleep(min(0.5 * attempt, 10))
            attempt += 1
            continue  # 继续重试

        if not invocations:
            break

        all_logs.update(invocations)
        page += 1
        print(f"✅ {account_id}/{service_name} 第{seg_id}段 第{page}页 {len(invocations)}条日志")

        # 计算下一页 offset
        offset = None
        for req_id in reversed(list(invocations.keys())):
            logs_list = invocations[req_id]
            if isinstance(logs_list, list) and logs_list:
                metadata = logs_list[-1].get("$metadata", {})
                offset = metadata.get("id")
                if offset:
                    break
        if not offset:
            break

    return all_logs



async def fetch_account(account_id, service_name, dates, sem_global: asyncio.Semaphore):
    sem_account = asyncio.Semaphore(float("inf"))
    async with aiohttp.ClientSession() as session:
        for date_str in dates:
            print(f"\n===== 抓取 {account_id}/{service_name} 的 {date_str} 日日志（UTC） =====")
            ranges = split_timeframes(date_str)
            all_logs = {}
            pending_segments = ranges.copy()
            paused_queue = asyncio.Queue()

            # 主线程抓第一段
            main_seg = pending_segments.pop(0)
            main_logs = await fetch_segment(session, account_id, service_name, 1, *main_seg, paused_queue)
            all_logs.update(main_logs)

            # 从线程抓剩余段
            tasks = {}
            for seg_id, (start_ms, end_ms) in enumerate(pending_segments, 2):
                await asyncio.sleep(FOLLOWER_START_INTERVAL)
                task = asyncio.create_task(fetch_segment(session, account_id, service_name, seg_id, start_ms, end_ms, sem_account, sem_global, paused_queue))
                tasks[seg_id] = task

            # 循环恢复暂停任务
            while not paused_queue.empty() or tasks:
                # 处理已完成任务
                for seg_id, task in list(tasks.items()):
                    if task.done():
                        try:
                            all_logs.update(task.result())
                        except Exception as e:
                            print(f"❌ {account_id}/{service_name} 第{seg_id}段异常: {e}")
                        tasks.pop(seg_id)

                # 恢复暂停任务
                while not paused_queue.empty():
                    seg_id, start_ms, end_ms = await paused_queue.get()
                    print(f"♻️ {account_id}/{service_name} 第{seg_id}段恢复任务")
                    task = asyncio.create_task(fetch_segment(session, account_id, service_name, seg_id, start_ms, end_ms, sem_account, sem_global, paused_queue))
                    tasks[seg_id] = task
                    await asyncio.sleep(FOLLOWER_RECOVERY_INTERVAL)

                await asyncio.sleep(1)

            # 保存 JSON
            out_file = f"{account_id}_invocations_{date_str}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump({"invocations": all_logs}, f, ensure_ascii=False, indent=2)
            print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")


async def main_async():
    args = sys.argv[1:]
    selected_days = next((int(a) for a in args if a.isdigit()), 1)
    selected_accounts = [a[1:] for a in args if a.startswith("-")]
    if selected_accounts:
        accounts = {k: v for k, v in ACCOUNTS.items() if k in selected_accounts}
    else:
        accounts = ACCOUNTS

    print(f"📅 查询天数: {selected_days}")
    print(f"👥 目标账户: {', '.join(accounts.keys())}")
    dates = get_date_list(str(selected_days))

    sem_global = asyncio.Semaphore(float("inf"))

    # 控制同时查询账户数
    account_list = list(accounts.items())
    for i in range(0, len(account_list), MAX_CONCURRENT_ACCOUNTS):
        batch = account_list[i:i + MAX_CONCURRENT_ACCOUNTS]
        tasks = [fetch_account(acc_id, svc_name, dates, sem_global) for acc_id, svc_name in batch]
        await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main_async())


