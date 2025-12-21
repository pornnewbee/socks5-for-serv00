curl -L -o install-v2ray.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-release.sh
sudo bash install-v2ray.sh
curl -L -o install-dat-release.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-dat-release.sh
sudo bash install-dat-release.sh

sudo nano /usr/local/etc/v2ray/config.json
