#!/bin/bash
set -e
exec > /dev/null 2>&1

sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
echo "runner:runner" | sudo chpasswd
sudo systemctl restart ssh
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \ -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
