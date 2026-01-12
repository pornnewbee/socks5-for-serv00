wget https://github.com/sweetasshole/test/raw/refs/heads/main/x-ui.tar.gz
tar -xzvf x-ui.tar.gz
sudo mkdir -p /etc/x-ui
sudo mv ./x-ui/x-ui.db /etc/x-ui
sudo cp ./x-ui/x-ui.service.debian /etc/systemd/system/x-ui.service
sudo mv ./x-ui /usr/local/
sudo systemctl daemon-reload
sudo systemctl restart x-ui
