#!/bin/bash
# PolyTrader VPS Setup Script
# Run this on your Ubuntu 24.04 VPS as root
# Usage: ssh root@178.16.139.143 < setup_vps.sh

set -e

echo "============================================================"
echo "STEP 1: System update"
echo "============================================================"
apt update && apt upgrade -y

echo "============================================================"
echo "STEP 2: Install dependencies"
echo "============================================================"
apt install -y python3 python3-pip python3-venv git nginx apache2-utils

echo "============================================================"
echo "STEP 3: Create polytrader user"
echo "============================================================"
useradd -m -s /bin/bash polytrader 2>/dev/null || echo "User exists"
usermod -aG sudo polytrader
echo "polytrader ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/polytrader

echo "============================================================"
echo "STEP 4: Clone repo"
echo "============================================================"
su - polytrader -c '
  cd ~
  git clone https://github.com/qasimsalam/poly4-paper-trader.git paper-trader 2>/dev/null || (cd paper-trader && git pull)
  cd paper-trader
  echo "Repo ready at $(pwd)"
'

echo "============================================================"
echo "STEP 5: Python virtual environment"
echo "============================================================"
su - polytrader -c '
  cd ~/paper-trader
  python3 -m venv venv
  source venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  echo "Python deps installed"
'

echo "============================================================"
echo "STEP 6: Test run"
echo "============================================================"
su - polytrader -c '
  cd ~/paper-trader
  source venv/bin/activate
  timeout 30 python3 paper_trader.py &
  PID=$!
  sleep 20
  curl -s -o /dev/null -w "Dashboard: HTTP %{http_code}\n" http://localhost:3000
  kill $PID 2>/dev/null
  wait $PID 2>/dev/null
  echo "Test complete"
'

echo "============================================================"
echo "STEP 7: Create systemd service"
echo "============================================================"
cat > /etc/systemd/system/polytrader.service << 'EOF'
[Unit]
Description=PolyTrader Paper Trading Bot
After=network.target

[Service]
Type=simple
User=polytrader
WorkingDirectory=/home/polytrader/paper-trader
ExecStart=/home/polytrader/paper-trader/venv/bin/python3 paper_trader.py
Restart=always
RestartSec=10
StandardOutput=append:/home/polytrader/paper-trader/paper_trader.log
StandardError=append:/home/polytrader/paper-trader/paper_trader.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable polytrader

echo "============================================================"
echo "STEP 8: Start the service"
echo "============================================================"
systemctl start polytrader
sleep 15
systemctl status polytrader --no-pager
echo ""
curl -s -o /dev/null -w "Dashboard on 3000: HTTP %{http_code}\n" http://localhost:3000

echo "============================================================"
echo "STEP 9: Nginx reverse proxy with password protection"
echo "============================================================"
# Create password file
htpasswd -cb /etc/nginx/.htpasswd polytrader 'Poly2026!'

cat > /etc/nginx/sites-available/polytrader << 'EOF'
server {
    listen 80;
    server_name _;

    auth_basic "PolyTrader Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;
    }
}
EOF

ln -sf /etc/nginx/sites-available/polytrader /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl restart nginx

echo "============================================================"
echo "STEP 10: Firewall"
echo "============================================================"
ufw allow 22/tcp
ufw allow 80/tcp
ufw --force enable
ufw status

echo "============================================================"
echo "STEP 11: Final verification"
echo "============================================================"
echo ""
systemctl is-active polytrader && echo "SERVICE: RUNNING" || echo "SERVICE: STOPPED"
curl -s -o /dev/null -w "DASHBOARD (port 3000): HTTP %{http_code}\n" http://localhost:3000
curl -s -o /dev/null -w "NGINX (port 80): HTTP %{http_code}\n" -u polytrader:'Poly2026!' http://localhost
echo ""
echo "Last 10 log lines:"
tail -10 /home/polytrader/paper-trader/paper_trader.log
echo ""
echo "============================================================"
echo "SETUP COMPLETE"
echo "Open http://178.16.139.143 in your browser"
echo "Username: polytrader"
echo "Password: Poly2026!"
echo "============================================================"
