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
#   ./deploy_gcp.sh              # First-time deploy (create VM)
#   ./deploy_gcp.sh update       # Update existing VM (git pull + restart)
#   ./deploy_gcp.sh logs         # View live logs
#   ./deploy_gcp.sh ssh          # SSH into VM
#   ./deploy_gcp.sh stop         # Stop VM
#   ./deploy_gcp.sh start        # Start VM
# =====================================================

set -e

PROJECT_NAME="algo-trading-bot"
ZONE="asia-south1-a"  # Mumbai (closest to NSE)
MACHINE_TYPE="e2-small"  # 2 vCPU, 2GB RAM (~Rs 800/month)
REPO_URL="https://github.com/voletiramu/survival_strategy"
CRED_FILE_LOCAL="C:/Users/Ram/Data/Angel/ANGEL_API_KEY=your_api_key.txt"
CRED_FILE_REMOTE="/home/ram/angel_creds.txt"

echo "============================================"
echo "  ALGO TRADING - GCP DEPLOYMENT"
echo "============================================"

# --- UPDATE MODE ---
if [ "$1" = "update" ]; then
    echo ""
    echo "Updating existing VM..."
    gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command="
        cd /home/ram/algo_trading &&
        git pull origin master &&
        pip3 install -r requirements.txt &&
        sudo systemctl restart algo-trading &&
        echo 'Update complete! Service restarted.' &&
        sudo systemctl status algo-trading --no-pager
    "
    echo ""
    echo "Update complete!"
    exit 0
fi

# --- LOGS MODE ---
if [ "$1" = "logs" ]; then
    echo "Streaming logs from VM..."
    gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command="sudo journalctl -u algo-trading -f"
    exit 0
fi

# --- SSH MODE ---
if [ "$1" = "ssh" ]; then
    gcloud compute ssh $PROJECT_NAME --zone=$ZONE
    exit 0
fi

# --- STOP MODE ---
if [ "$1" = "stop" ]; then
    echo "Stopping VM..."
    gcloud compute instances stop $PROJECT_NAME --zone=$ZONE
    echo "VM stopped."
    exit 0
fi

# --- START MODE ---
if [ "$1" = "start" ]; then
    echo "Starting VM..."
    gcloud compute instances start $PROJECT_NAME --zone=$ZONE
    echo "VM started."
    exit 0
fi

# --- FIRST-TIME DEPLOY ---
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
# ===== AUTO-INSTALL ON FIRST BOOT =====
apt-get update
apt-get install -y python3.12 python3-pip git

# Clone repo
git clone '"$REPO_URL"' /home/ram/algo_trading
cd /home/ram/algo_trading
pip3 install -r requirements.txt

# Set timezone
timedatectl set-timezone Asia/Kolkata

# Create systemd service (Threaded: 45s equity, 30s commodity)
cat > /etc/systemd/system/algo-trading.service << EOF
[Unit]
Description=Algo Trading Paper Trader (Threaded)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/ram/algo_trading
ExecStart=/usr/bin/python3 run_paper_trade.py --equity-interval 0.75 --commodity-interval 0.5
Restart=always
RestartSec=30
Environment=TZ=Asia/Kolkata
Environment=ANGEL_CRED_FILE=/home/ram/angel_creds.txt
Environment=TRADING_INSTANCE=gcloud
StandardOutput=append:/var/log/algo-trading.log
StandardError=append:/var/log/algo-trading-error.log
MemoryMax=1500M

[Install]
WantedBy=multi-user.target
EOF

# Log rotation
cat > /etc/logrotate.d/algo-trading << LOGEOF
/var/log/algo-trading*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
LOGEOF

# Enable and start service
systemctl daemon-reload
systemctl enable algo-trading
systemctl start algo-trading

echo "Algo trading service started!"
'

echo ""
echo "Step 2: VM created! Waiting 60s for startup script to complete..."
sleep 60

# Step 3: Copy Angel API credentials
echo ""
echo "Step 3: Copying Angel API credentials to VM..."
if [ -f "$CRED_FILE_LOCAL" ]; then
    gcloud compute scp "$CRED_FILE_LOCAL" $PROJECT_NAME:$CRED_FILE_REMOTE --zone=$ZONE
    echo "  Credentials copied to $CRED_FILE_REMOTE"
else
    echo "  WARNING: Local credential file not found at $CRED_FILE_LOCAL"
    echo "  You will need to manually copy credentials to the VM."
fi

# Step 4: Restart service to pick up credentials
echo ""
echo "Step 4: Restarting service with credentials..."
gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command="sudo systemctl restart algo-trading"
sleep 5

# Step 5: Verify
echo ""
echo "Step 5: Verifying deployment..."
gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command="sudo systemctl status algo-trading --no-pager"

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE!"
echo "============================================"
echo ""
echo "  VM: $PROJECT_NAME"
echo "  Zone: $ZONE"
echo "  Machine: $MACHINE_TYPE (~Rs 800/month)"
echo ""
echo "  Scan Intervals:"
echo "    Equity:    45 seconds (NIFTY, BANKNIFTY, SENSEX)"
echo "    Commodity: 30 seconds (GOLDM, SILVERM, CRUDEOILM)"
echo "    Crypto:    5 minutes  (BTC, ETH, SOL)"
echo ""
echo "  Commands:"
echo "    Update:  ./deploy_gcp.sh update"
echo "    Logs:    ./deploy_gcp.sh logs"
echo "    SSH:     ./deploy_gcp.sh ssh"
echo "    Stop:    ./deploy_gcp.sh stop"
echo "    Start:   ./deploy_gcp.sh start"
echo ""
echo "  Direct commands:"
echo "    SSH:     gcloud compute ssh $PROJECT_NAME --zone=$ZONE"
echo "    Logs:    gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command='journalctl -u algo-trading -f'"
echo "    Status:  gcloud compute ssh $PROJECT_NAME --zone=$ZONE --command='systemctl status algo-trading'"
echo "============================================"
