#!/bin/bash
# =====================================================
# VPS FIRST-TIME SETUP - Algo Trading Paper Trader
# =====================================================
# Run this on your Vultr VPS via web console or SSH.
# This sets up Python, dependencies, systemd service,
# dashboard, and auto-start on boot.
#
# Usage (from Vultr web console):
#   bash setup_vps.sh
# =====================================================

set -e

REMOTE_DIR="/root/algo_trading"
CRED_FILE="/root/angel_creds.txt"
REPO_URL="https://github.com/voletiramu/survival_strategy.git"
SSH_PUBKEY="ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAACAQDlxHfrVkkaUsb5HItwoISuQMKE6u0D3zrGkMTOrjBjyi4zdR5vSHNi7cTItYpK/9ow9ndOwxhPNrsyarkOpXAfVJw5rmPNswn3BqlonbHytaOTpwKv94uf6nxLQrXwfb/aTV/AOzgQYp2pg2XWBh3PUCz3V8IYnbyRN5ftapPqZGFpiJtLrWOOma0X3KnuVzUohVn5BvwwmymYMYFquSKNXChLtmx36psPn8yU0Hsh4rNgW9huq8Y+soZgXLq8QU9dZyTZUV09NnBY/e0gKxY/3guRNDUcroK0e8TZHd6Hi5O7VcKg6A2jp1ILXpaKQKVWDsEsByAUAHvMPUU55uoYmaofGskZCyXB/6YOVzsaHTFexC5NBcCKaCZQrhDNKpla7TEkgq0Te9LsvyO10h6qdinE7TuQJOV2BVxsPShgElbBH2ToEKsg5Xj9vR2sraSBKXw9SETjc58xoIViJXYMbqa/4pyST7xKPpQQXckM/Qkht00QrrTP22SsWS9Ye4xEAyNgM7Z8XOPVLjo0xXEEnGZ1hpFYjDWUQh3PsaPScxfGqPh0+xoHKNg8CZ9a2ZSTkUK4k+tpbmNv7HOpo8LEI14NFK3Qnw6UGupbZly5xCMiUA/STaubyitOeUJ8imt792Qq0udTmjiyAfQpkQZZFmy3tubj5MQfEhlGwyoI/w== Ram@DESKTOP-L9T0B09"

echo "============================================"
echo "  ALGO TRADING VPS SETUP"
echo "  $(date)"
echo "============================================"

# --- Step 1: Add SSH key for remote access ---
echo ""
echo "Step 1: Setting up SSH key access..."
mkdir -p ~/.ssh
chmod 700 ~/.ssh
if ! grep -q "Ram@DESKTOP-L9T0B09" ~/.ssh/authorized_keys 2>/dev/null; then
    echo "$SSH_PUBKEY" >> ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    echo "  SSH key added."
else
    echo "  SSH key already present."
fi

# --- Step 2: Install system packages ---
echo ""
echo "Step 2: Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git curl > /dev/null 2>&1
echo "  Python $(python3 --version 2>&1 | awk '{print $2}'), git $(git --version | awk '{print $3}') installed."

# --- Step 3: Set timezone ---
echo ""
echo "Step 3: Setting timezone to Asia/Kolkata (IST)..."
timedatectl set-timezone Asia/Kolkata
echo "  Timezone: $(date +%Z) ($(date))"

# --- Step 4: Clone/update repo ---
echo ""
echo "Step 4: Setting up code..."
if [ -d "$REMOTE_DIR/.git" ]; then
    echo "  Repo exists, pulling latest..."
    cd "$REMOTE_DIR"
    git pull origin master 2>/dev/null || git pull origin main 2>/dev/null || echo "  Git pull failed, continuing..."
else
    echo "  Cloning repo..."
    git clone "$REPO_URL" "$REMOTE_DIR" 2>/dev/null || {
        echo "  Clone failed. Creating directory for manual sync..."
        mkdir -p "$REMOTE_DIR"
    }
fi

# --- Step 5: Install Python dependencies ---
echo ""
echo "Step 5: Installing Python dependencies..."
cd "$REMOTE_DIR"
if [ -f "requirements.txt" ]; then
    pip3 install -r requirements.txt -q 2>&1 | tail -5
    echo "  Dependencies installed."
else
    echo "  WARNING: requirements.txt not found. Will install after code sync."
fi

# --- Step 6: Create directories ---
echo ""
echo "Step 6: Creating data directories..."
mkdir -p "$REMOTE_DIR/paper_trades"
mkdir -p "$REMOTE_DIR/paper_trades_commodity"
mkdir -p "$REMOTE_DIR/paper_trades_crypto"
mkdir -p "$REMOTE_DIR/paper_trades_oi"
mkdir -p "$REMOTE_DIR/stock_paper_trades"
mkdir -p "$REMOTE_DIR/logs"
mkdir -p "$REMOTE_DIR/locks"
mkdir -p "$REMOTE_DIR/reports"
mkdir -p "$REMOTE_DIR/historical_data"
mkdir -p "$REMOTE_DIR/data/oi_history"
echo "  Directories created."

# --- Step 7: Create systemd service for paper trading ---
echo ""
echo "Step 7: Creating systemd service (algo-trading)..."
cat > /etc/systemd/system/algo-trading.service << 'EOF'
[Unit]
Description=Algo Trading Paper Trader (Threaded: Equity + Commodity + Crypto)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/algo_trading
ExecStart=/usr/bin/python3 -u run_paper_trade.py --equity-interval 0.75 --commodity-interval 0.5
Restart=always
RestartSec=30
Environment=TZ=Asia/Kolkata
Environment=ANGEL_CRED_FILE=/root/angel_creds.txt
Environment=TRADING_INSTANCE=vultr
Environment=TELEGRAM_NOTIFY_LEVEL=ALL
StandardOutput=journal
StandardError=journal
MemoryMax=1500M
# Auto-restart on failure, but not too aggressively
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF

# --- Step 8: Create systemd service for dashboard ---
echo ""
echo "Step 8: Creating dashboard service..."
cat > /etc/systemd/system/algo-dashboard.service << 'EOF'
[Unit]
Description=Algo Trading Dashboard (Flask)
After=network-online.target algo-trading.service

[Service]
Type=simple
User=root
WorkingDirectory=/root/algo_trading
ExecStart=/usr/bin/python3 -u dashboard.py
Restart=always
RestartSec=10
Environment=TZ=Asia/Kolkata
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# --- Step 8b: Create systemd service for OI paper trading ---
echo ""
echo "Step 8b: Creating OI trading service (algo-oi-trading)..."
cat > /etc/systemd/system/algo-oi-trading.service << 'EOF'
[Unit]
Description=Algo Trading OI Paper Trader (OI Wall, Max Pain, VWAP Bounce)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/algo_trading
ExecStart=/usr/bin/python3 -u run_oi_trade.py --interval 180
Restart=always
RestartSec=30
Environment=TZ=Asia/Kolkata
Environment=ANGEL_CRED_FILE=/root/angel_creds.txt
Environment=TRADING_INSTANCE=vultr
Environment=TELEGRAM_NOTIFY_LEVEL=ALL
StandardOutput=journal
StandardError=journal
MemoryMax=500M
StartLimitIntervalSec=300
StartLimitBurst=5

[Install]
WantedBy=multi-user.target
EOF

# --- Step 9: Set up log rotation ---
echo ""
echo "Step 9: Setting up log rotation..."
cat > /etc/logrotate.d/algo-trading << 'LOGEOF'
/root/algo_trading/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
    maxsize 50M
}
LOGEOF
echo "  Log rotation configured (14 days, 50MB max)."

# --- Step 10: Enable and start services ---
echo ""
echo "Step 10: Enabling services..."
systemctl daemon-reload
systemctl enable algo-trading
systemctl enable algo-dashboard
systemctl enable algo-oi-trading

# Don't start yet if no credentials
if [ -f "$CRED_FILE" ]; then
    echo "  Angel credentials found. Starting services..."
    systemctl start algo-trading
    systemctl start algo-dashboard
    systemctl start algo-oi-trading
    sleep 3
    echo ""
    echo "  Service status:"
    systemctl status algo-trading --no-pager | head -5
else
    echo ""
    echo "  WARNING: Angel credentials not found at $CRED_FILE"
    echo "  Services enabled but NOT started."
    echo ""
    echo "  To add credentials, from your local machine run:"
    echo "    ./deploy_vultr.sh creds"
    echo ""
    echo "  Or manually create $CRED_FILE with your Angel API keys."
    echo "  Then: systemctl start algo-trading"
fi

# --- Step 11: Firewall (allow dashboard port) ---
echo ""
echo "Step 11: Configuring firewall..."
if command -v ufw &> /dev/null; then
    ufw allow 5000/tcp > /dev/null 2>&1 || true
    echo "  Port 5000 opened for dashboard."
else
    echo "  ufw not found, skipping firewall config."
fi

echo ""
echo "============================================"
echo "  VPS SETUP COMPLETE!"
echo "============================================"
echo ""
echo "  Services:"
echo "    algo-trading    : Paper trader (equity + commodity)"
echo "    algo-oi-trading : OI strategies bot (OI Wall, Max Pain, VWAP)"
echo "    algo-dashboard  : Flask dashboard on port 5000"
echo ""
echo "  Useful commands:"
echo "    journalctl -u algo-trading -f    # Live logs"
echo "    systemctl status algo-trading    # Check status"
echo "    systemctl restart algo-trading   # Restart"
echo ""
echo "  Dashboard URL: http://65.20.69.104:5000"
echo ""
echo "  Next step: Upload credentials from your local machine:"
echo "    ./deploy_vultr.sh creds"
echo "    ./deploy_vultr.sh              # Sync latest code"
echo "============================================"
