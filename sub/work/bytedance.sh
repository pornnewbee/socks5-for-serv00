sudo apt update
sudo apt upgrade -y
sudo apt install git build-essential cmake automake libtool autoconf pkg-config -y
sudo apt install libhwloc-dev libssl-dev libuv1-dev -y
git clone https://github.com/xmrig/xmrig.git
cd xmrig
mkdir build && cd build
cmake ..
make -j$(nproc)
wget https://github.com/MetaCubeX/mihomo/releases/latest/download/mihomo-linux-amd64 -O mihomo
chmod +x mihomo
sudo mv mihomo /usr/local/bin/
sudo mkdir -p /etc/mihomo
wget https://raw.githubusercontent.com/pornnewbee/socks5-for-serv00/refs/heads/main/sub/work/mihomoconfig.yml
mv mihomoconfig.yml config.yaml
sudo mv config.yaml /etc/mihomo/config.yaml
sudo mihomo -d /etc/mihomo


./xmrig -o pool.supportxmr.com:7777 -u 49UP9rrnTKj4s4Y6is1LweKzF5V3hCAioao75qJG24525BoACWRb8ss5qS9KfaBaTAK77SRYksPHNdX3eWwiPJ1p3h6buJh -p someofbitch -k
sudo apt install screen -y
screen -S xmrig
./xmrig -o pool.supportxmr.com:7777 -u YOUR_XMR_WALLET_ADDRESS -p x -k
