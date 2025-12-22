curl -L -o install-v2ray.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-release.sh
sudo bash install-v2ray.sh
curl -L -o install-dat-release.sh https://raw.githubusercontent.com/v2fly/fhs-install-v2ray/master/install-dat-release.sh
sudo bash install-dat-release.sh

sudo nano /usr/local/etc/v2ray/config.json
{
  "inbounds": [
    {
      "port": 80,
      "protocol": "vless",
      "settings": {
        "clients": [
          {
            "id": "e7a1c123-4567-89ab-cdef-0123456789ab",
            "level": 0,
            "flow": ""
          }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "ws",
        "wsSettings": {
          "path": "/"
        }
      }
    }
  ],
  "outbounds": [
    {
      "protocol": "freedom",
      "settings": {}
    }
  ]
}


sudo systemctl restart v2ray
sudo systemctl status v2ray
sudo journalctl -u v2ray -f
