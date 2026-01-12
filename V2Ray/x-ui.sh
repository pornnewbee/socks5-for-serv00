wget https://github.com/sweetasshole/test/raw/refs/heads/main/x-ui.tar.gz
tar -xzvf x-ui.tar.gz
sudo mkdir /etc/x-ui
sudo mv ./x-ui/x-ui.db /etc/x-ui
sudo mv ./x-ui/x-ui.service.debian /etc/systemd/system/x-ui.service
sudo mv ./x-ui /usr/local/
systemctl reload
systemctl restart x-ui
