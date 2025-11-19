#!/usr/bin/env python3
# coding: utf-8

import os, sys, json, copy, asyncio, aiohttp, contextlib
from datetime import datetime, timedelta, timezone

# ===================== 配置区 =====================
SEGMENTS_PER_DAY = 48
FOLLOWER_START_INTERVAL = 1
FOLLOWER_RECOVERY_INTERVAL = 1
ADAPTIVE_SEMAPHORE_INIT = 1   # 启动时初始最大并发请求
ADAPTIVE_SEMAPHORE_MIN = 1
ADAPTIVE_SEMAPHORE_MAX = 20
ADAPTIVE_SEMAPHORE_INC = 1    # 成功慢慢恢复并发
ADAPTIVE_SEMAPHORE_DEC = 1    # 遇到429降低并发
# ==================================================

# 环境变量
ACCOUNTS_JSON = os.getenv("ACCOUNTS_JSON")
LOCAL_COOKIE = os.getenv("CF_COOKIE") or ""

if not ACCOUNTS_JSON or not LOCAL_COOKIE or len(LOCAL_COOKIE) < 20:
    print("❌ 请确保环境变量 ACCOUNTS_JSON 和 CF_COOKIE 已设置且有效")
    sys.exit(1)

try:
    ACCOUNTS = json.loads(ACCOUNTS_JSON)
except Exception as e:
    print("❌ ACCOUNTS_JSON 内容无效:", e)
    sys.exit(1)

URL_TEMPLATE = "https://dash.cloudflare.com/api/v4/accounts/{account_id}/workers/observability/telemetry/query"
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

# ===================== 工具函数 =====================
def get_date_list(arg: str):
    if arg.startswith("-") and arg[1:].isdigit():
        n = int(arg[1:])
        today = datetime.now(timezone.utc).date()
        return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n, 0, -1)]
    elif arg.isdigit():
        days_ago = int(arg)
        today = datetime.now(timezone.utc).date()
        return [(today - timedelta(days=days_ago)).strftime("%Y%m%d")]
    else:
        n = int(arg) if arg and arg.isdigit() else 7
        today = datetime.now(timezone.utc).date()
        return [(today - timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]

def split_timeframes(date_str):
    dt = datetime.strptime(date_str, "%Y%m%d")
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1) - timedelta(milliseconds=1)
    step = (int((end - start).total_seconds() * 1000)) // SEGMENTS_PER_DAY
    arr = []
    for i in range(SEGMENTS_PER_DAY):
        s = int(start.timestamp() * 1000) + i * step
        e = s + step if i < SEGMENTS_PER_DAY-1 else int(end.timestamp() * 1000)
        arr.append((s,e))
    return arr

def linear_delay(attempt: int):
    return min(0.5 * attempt, 10.0)

# ===================== 异步抓取 =====================
async def fetch_task(session, account_id, service_name, seg_id, start_ms, end_ms, queue, semaphore, offset=None):
    all_logs = {}
    page = 0
    base_data = {
        "view":"invocations",
        "queryId":"workers-logs-invocations",
        "limit":100,
        "parameters":{"datasets":["cloudflare-workers"],"filters":[{"key":"$metadata.service","type":"string","value":service_name,"operation":"eq"}],"calculations":[],"groupBys":[],"havings":[]},
        "timeframe":{"from":start_ms,"to":end_ms}
    }
    while True:
        data = copy.deepcopy(base_data)
        if offset:
            data["offset"] = offset
        attempt = 1
        async with semaphore:
            while True:
                try:
                    async with session.post(URL_TEMPLATE.format(account_id=account_id), headers=HEADERS, json=data, timeout=30) as resp:
                        status = resp.status
                        text = await resp.text()
                        if status == 200:
                            result = await resp.json()
                            break
                        elif status == 429:
                            print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 第{page+1}页 429暂停")
                            # 降低 semaphore
                            if semaphore._value > ADAPTIVE_SEMAPHORE_MIN:
                                semaphore._value -= ADAPTIVE_SEMAPHORE_DEC
                            await queue.put((seg_id,start_ms,end_ms,offset))
                            return all_logs
                        else:
                            print(f"⚠️ {account_id}/{service_name} 第{seg_id}段 HTTP {status} {text[:300]}")
                except Exception as e:
                    print(f"❌ {account_id}/{service_name} 第{seg_id}段 网络异常: {e}")
                delay = linear_delay(attempt)
                await asyncio.sleep(delay)
                attempt += 1

        if not result or "result" not in result or "invocations" not in result["result"]:
            break
        invocations = result["result"]["invocations"]
        if not invocations:
            break
        all_logs.update(invocations)
        page += 1
        # 计算下一页 offset
        offset = None
        for req_id in reversed(list(invocations.keys())):
            logs_list = invocations[req_id]
            if isinstance(logs_list,list) and logs_list:
                metadata = logs_list[-1].get("$metadata",{})
                offset = metadata.get("id")
                if offset: break
        # 成功慢慢恢复 semaphore
        if semaphore._value < ADAPTIVE_SEMAPHORE_MAX:
            semaphore._value += ADAPTIVE_SEMAPHORE_INC
        if not offset:
            break
    return all_logs

async def fetch_account(account_id, service_name, dates):
    async with aiohttp.ClientSession() as session:
        for date_str in dates:
            print(f"\n===== {account_id}/{service_name} {date_str} =====")
            segments = split_timeframes(date_str)
            queue = asyncio.Queue()
            for idx,(s,e) in enumerate(segments,1):
                await queue.put((idx,s,e,None))
            semaphore = asyncio.Semaphore(ADAPTIVE_SEMAPHORE_INIT)
            all_logs = {}
            tasks = []

            async def worker_loop():
                while not queue.empty():
                    seg_id,s,e,offset = await queue.get()
                    logs = await fetch_task(session, account_id, service_name, seg_id,s,e,queue,semaphore,offset)
                    if logs: all_logs.update(logs)
                    await asyncio.sleep(FOLLOWER_RECOVERY_INTERVAL)
            # 启动若干 worker
            for _ in range(ADAPTIVE_SEMAPHORE_INIT):
                tasks.append(asyncio.create_task(worker_loop()))
            await asyncio.gather(*tasks)
            # 保存文件
            out_file = f"{account_id}_invocations_{date_str}.json"
            with open(out_file,"w",encoding="utf-8") as f:
                json.dump({"invocations":all_logs},f,ensure_ascii=False,indent=2)
            print(f"📦 {account_id} 已保存 {len(all_logs)} 条日志 -> {out_file}")

# ===================== 主程序 =====================
async def main_async():
    args = sys.argv[1:]
    selected_days = "7"
    selected_accounts = []
    for a in args:
        if a.startswith("-"):
            selected_accounts.append(a[1:])
        elif a.isdigit():
            selected_days = a
    if selected_accounts:
        accounts = {k:v for k,v in ACCOUNTS.items() if k in selected_accounts}
    else:
        accounts = ACCOUNTS
    dates = get_date_list(selected_days)
    for acc_id, svc_name in accounts.items():
        await fetch_account(acc_id, svc_name, dates)

if __name__ == "__main__":
    asyncio.run(main_async())
