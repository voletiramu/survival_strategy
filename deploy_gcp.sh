#!/bin/bash
# =====================================================
# GCP DEPLOYMENT SCRIPT - Algo Trading Paper Trader
# =====================================================
# Prerequisites:
#   1. GCP account with billing enabled
#   2. gcloud CLI installed: https://cloud.google.com/sdk/docs/install
#   3. Authenticated: gcloud auth login
#
# Usage:
#   chmod +x deploy_gcp.sh
#   ./deploy_gcp.sh
# =====================================================

set -e

PROJECT_NAME="algo-trading-bot"
ZONE="asia-south1-a"  # Mumbai (closest to NSE)
MACHINE_TYPE="e2-small"  # 2 vCPU, 2GB RAM (~Rs 800/month)
REPO_URL="https://github.com/voletiramu/survival_strategy"

echo "============================================"
echo "  ALGO TRADING - GCP DEPLOYMENT"
echo "============================================"

# Step 1: Create VM Instance
echo ""
echo "Step 1: Creating GCP VM..."
gcloud compute instances create $PROJECT_NAME \
    --zone=$ZONE \
    --machine-type=$MACHINE_TYPE \
    --image-family=ubuntu-2204-lts \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=20GB \
    --tags=algo-trading \
    --metadata=startup-script='#!/bin/bash
# Auto-install on first boot
apt-get update
apt-get install -y python3.12 python3-pip git
pip3 install --upgrade pip

# Clone repo
git clone '"$REPO_URL"' /home/ram/algo_trading
cd /home/ram/algo_trading
pip3 install -r requirements.txt

# Set timezone
timedatectl set-timezone Asia/Kolkata

# Create systemd service
cat > /etc/systemd/system/algo-trading.service << EOF
[Unit]
Description=Algo Trading Paper Trader
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/ram/algo_trading
ExecStart=/usr/bin/python3 run_paper_trade.py
Restart=always
RestartSec=30
Environment=TZ=Asia/Kolkata
StandardOutput=append:/var/log/algo-trading.log
StandardError=append:/var/log/algo-trading-error.log

[Install]
WantedBy=multi-user.target
EOF

# Enable and start service
systemctl daemon-reload
systemctl enable algo-trading
systemctl start algo-trading

echo "Algo trading service started!"
'

echo ""
echo "Step 2: VM created! Waiting for startup..."
sleep 30

# Step 2: Verify
echo ""
echo "Step 3: Verifying deployment..."
gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command="systemctl status algo-trading"

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE!"
echo "============================================"
echo ""
echo "  VM: $PROJECT_NAME"
echo "  Zone: $ZONE"
echo "  Machine: $MACHINE_TYPE"
echo ""
echo "  Useful commands:"
echo "    SSH: gcloud compute ssh $PROJECT_NAME --zone=$ZONE"
echo "    Logs: gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command='journalctl -u algo-trading -f'"
echo "    Stop: gcloud compute instances stop $PROJECT_NAME --zone=$ZONE"
echo "    Start: gcloud compute instances start $PROJECT_NAME --zone=$ZONE"
echo ""
echo "  Cost: ~Rs 800/month (e2-small)"
echo "  Free tier: e2-micro (1 vCPU, 1GB) is free for 1 month"
echo "============================================"
