#!/bin/bash
set -e

echo "[*] setup env"
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo echo "runner:runner" | chpasswd
sudo systemctl restart ssh


echo "[*] Prepare directories"
sudo mkdir -p /mnt/repo/test
sudo chmod 777 /mnt/repo/test



echo "[+] env and directories finished successfully"
