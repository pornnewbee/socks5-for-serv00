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
        if content.startswith("{"):  # JSON 格式
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
        else:  # 纯文本格式 ip:port:protocol
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(":")
                if len(parts) != 3:
                    continue
                ip, port, proto = parts
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

def test_proxy(proxy):
    if not check_tcp(proxy):
        return None
    if not check_http(proxy):
        return None
    return f"{proxy['ip']}:{proxy['port']}:{proxy['protocol']}"

def main(file_path):
    proxies = load_proxies(file_path)
    valid = []
    with ThreadPoolExecutor(max_workers=THREADS) as executor:
        for result in executor.map(test_proxy, proxies):
            if result:
                valid.append(result)
    print(f"✅ 有效代理 {len(valid)}/{len(proxies)}")
    for v in valid:
        print(v)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python check_proxies.py <代理文件>")
        sys.exit(1)
    main(sys.argv[1])
