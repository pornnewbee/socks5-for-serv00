curl -L -o install-v2ray.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-release.sh
sudo bash install-v2ray.sh
curl -L -o install-dat-release.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-dat-release.sh
sudo bash install-dat-release.sh
rm install-v2ray.sh
rm install-dat-release.sh

wget https://raw.githubusercontent.com/pornnewbee/socks5-for-serv00/refs/heads/main/V2Ray/config.json
mv config.json /usr/local/etc/v2ray/

sudo systemctl restart v2ray
sudo systemctl status v2ray

wget https://github.com/sweetasshole/test/raw/refs/heads/main/x-ui.tar.gz
tar -xzvf x-ui.tar.gz
sudo mkdir -p /etc/x-ui
sudo mv ./x-ui/x-ui.db /etc/x-ui
sudo cp ./x-ui/x-ui.service.debian /etc/systemd/system/x-ui.service
sudo mv ./x-ui /usr/local/
sudo systemctl daemon-reload
sudo systemctl restart x-ui
