#!/bin/bash
set -euo pipefail

# ===== 配置区 =====
USER_NAME="runner"
KEY_URL="https://raw.githubusercontent.com/sweetasshole/test/refs/heads/main/id_ed25519.pub"

ENABLE_PASSWORD_AUTH="yes"
ENABLE_ROOT_LOGIN="yes"
SET_USER_PASSWORD="yes"
USER_PASSWORD="runner"

INSTALL_CLOUDFLARED="yes"
INSTALL_CFAGENT="yes"

SILENT="yes"
# ==================

# ===== 静默模式 =====
if [[ "$SILENT" == "yes" ]]; then
  exec > /dev/null 2>&1
fi

echo "[*] Start deploy"

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

while IFS= read -r key; do
  grep -qxF "$key" "$AUTH_KEYS" || echo "$key" >> "$AUTH_KEYS"
done <<< "$KEYS"

echo "[+] SSH public key deployed"

# ===== SSH 配置 =====
echo "[*] Configuring sshd"

sed -i "s/^#\?PasswordAuthentication.*/PasswordAuthentication $ENABLE_PASSWORD_AUTH/" /etc/ssh/sshd_config
sed -i "s/^#\?PubkeyAuthentication.*/PubkeyAuthentication yes/" /etc/ssh/sshd_config
sed -i "s/^#\?PermitRootLogin.*/PermitRootLogin $ENABLE_ROOT_LOGIN/" /etc/ssh/sshd_config

if [[ "$SET_USER_PASSWORD" == "yes" ]]; then
  echo "$USER_NAME:$USER_PASSWORD" | chpasswd
fi

systemctl restart ssh || systemctl restart sshd || true

apt update -y
apt install -y nload curl

echo "[+] SSH service restarted"

# ===== 安装 cloudflared =====
if [[ "$INSTALL_CLOUDFLARED" == "yes" ]]; then
  echo "[*] Installing cloudflared"

  curl -fsSL \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o /usr/local/bin/cloudflared

  chmod +x /usr/local/bin/cloudflared

  echo "[+] cloudflared installed"
fi

# ===== 创建 cfagent + 最严格 sudo 限制 =====
if [[ "$INSTALL_CFAGENT" == "yes" ]]; then

  CFAGENT_USER="cfagent"
  SUDOERS_FILE="/etc/sudoers.d/cfagent"

  echo "[*] Setting up cfagent"

  if ! id "$CFAGENT_USER" >/dev/null 2>&1; then
      useradd --system --create-home --shell /usr/sbin/nologin "$CFAGENT_USER"
      echo "[+] User created: $CFAGENT_USER"
  else
      echo "[*] User exists: $CFAGENT_USER"
  fi

  CF_BIN=$(command -v cloudflared || true)

  if [[ -z "$CF_BIN" ]]; then
      echo "[!] cloudflared not found, skip cfagent sudo setup"
  else
      echo "[*] cloudflared path: $CF_BIN"

      cat > "$SUDOERS_FILE" <<EOF
Defaults:$CFAGENT_USER secure_path="/usr/local/bin:/usr/bin:/bin"

User_Alias CFAGENT = $CFAGENT_USER

Cmnd_Alias CF_TUNNEL = $CF_BIN tunnel *
Cmnd_Alias CF_SERVICE = $CF_BIN service *

CFAGENT ALL=(root) NOPASSWD: CF_TUNNEL, CF_SERVICE
EOF

      chmod 440 "$SUDOERS_FILE"

      echo "[+] cfagent sudo rules installed"
  fi
fi

echo "[✓] All done"
