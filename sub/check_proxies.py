import json
import socket
import requests
from concurrent.futures import ThreadPoolExecutor

CHECK_URL = "https://www.gstatic.com/generate_204"
TIMEOUT = 5
THREADS = 50

def load_proxies(file_path):
    proxies = []
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read().strip()
        if not content:
            return proxies
        # JSON 格式
        if content.startswith("{"):
            try:
                data = json.loads(content)
                for item in data.get("data", []):
                    ip = item.get("ip")
                    port = int(item.get("port"))
                    proto_list = item.get("protocols", [])
                    if not proto_list:
                        continue
                    proto = proto_list[0].lower()
                    proxies.append({"ip": ip, "port": port, "protocol": proto})
            except Exception as e:
                print("JSON 解析失败:", e)
        else:
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 尝试 ip:port:protocol
                if ":" in line:
                    parts = line.split(":")
                    if len(parts) == 3:
                        ip, port, proto = parts
                        proxies.append({"ip": ip, "port": int(port), "protocol": proto.lower()})
                        continue
                # 尝试制表符分隔格式（ip\tport\tprotocol\t...）
                parts = line.split("\t")
                if len(parts) >= 3:
                    ip, port, proto = parts[:3]
                    proxies.append({"ip": ip, "port": int(port), "protocol": proto.lower()})
    return proxies

def check_tcp(proxy):
    try:
        with socket.create_connection((proxy["ip"], proxy["port"]), timeout=TIMEOUT):
            return True
    except:
        return False

def check_http(proxy):
    proto = proxy["protocol"]
    ip = proxy["ip"]
    port = proxy["port"]
    proxies_dict = {}
    if proto in ["http", "https"]:
        proxies_dict = {"http": f"http://{ip}:{port}", "https": f"http://{ip}:{port}"}
    elif proto in ["socks4", "socks5"]:
        proxies_dict = {"http": f"{proto}://{ip}:{port}", "https": f"{proto}://{ip}:{port}"}
    else:
        return False
    try:
        r = requests.get(CHECK_URL, proxies=proxies_dict, timeout=TIMEOUT)
        return r.status_code == 204
    except:
        return False

def main(file_path):
    proxies = load_proxies(file_path)

    # --- TCP 检测阶段 ---
    reachable_tcp = []
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        for proxy, result in zip(proxies, executor.map(check_tcp, proxies)):
            if result:
                reachable_tcp.append(proxy)
    print(f"🔹 TCP 可达代理 {len(reachable_tcp)}/{len(proxies)}")

    # --- 代理可用性检测阶段 ---
    valid = []
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        for proxy, result in zip(reachable_tcp, executor.map(check_http, reachable_tcp)):
            if result:
                valid.append(proxy)
    # 输出可用代理
    print(f"✅ 可用代理 {len(valid)}/{len(reachable_tcp)}")
    for v in valid:
        print(f"{v['ip']}:{v['port']}:{v['protocol']}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python check_proxies.py <代理文件>")
        sys.exit(1)
    main(sys.argv[1])
