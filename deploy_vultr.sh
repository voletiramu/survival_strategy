#!/bin/bash
# =====================================================
# VULTR DEPLOYMENT SCRIPT - Algo Trading Paper Trader
# =====================================================
# VPS: algo-trading-bot (65.20.69.104) - Mumbai
#
# FIRST TIME SETUP:
#   1. Go to Vultr web console for your VPS
#   2. Login as root
#   3. Run: curl -sL https://raw.githubusercontent.com/voletiramu/survival_strategy/master/setup_vps.sh | bash
#      OR paste the contents of setup_vps.sh manually
#   4. Then from your local machine: ./deploy_vultr.sh
#
# Usage:
#   ./deploy_vultr.sh              # Deploy/update code + restart ALL services
#   ./deploy_vultr.sh logs         # Stream live equity/commodity logs
#   ./deploy_vultr.sh status       # Check equity/commodity service status
#   ./deploy_vultr.sh ssh          # SSH into VPS
#   ./deploy_vultr.sh stop         # Stop equity/commodity service
#   ./deploy_vultr.sh start        # Start equity/commodity service
#   ./deploy_vultr.sh restart      # Restart equity/commodity service
#   ./deploy_vultr.sh creds        # Upload Angel API credentials
#   ./deploy_vultr.sh dashboard    # Check dashboard status
#   ./deploy_vultr.sh stock-logs   # Stream live stock bot logs
#   ./deploy_vultr.sh stock-status # Check stock bot status
#   ./deploy_vultr.sh stock-stop   # Stop stock bot
#   ./deploy_vultr.sh stock-start  # Start stock bot
#   ./deploy_vultr.sh stock-restart # Restart stock bot
#   ./deploy_vultr.sh oi-logs      # Stream live OI bot logs
#   ./deploy_vultr.sh oi-status    # Check OI bot status
#   ./deploy_vultr.sh oi-stop      # Stop OI bot
#   ./deploy_vultr.sh oi-start     # Start OI bot
#   ./deploy_vultr.sh oi-restart   # Restart OI bot
#   ./deploy_vultr.sh all-status   # Check ALL services
# =====================================================

set -e

VPS_IP="65.20.69.104"
VPS_USER="root"
SSH_KEY="$HOME/.ssh/id_rsa_vultr"
REMOTE_DIR="/root/algo_trading"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
CRED_FILE_LOCAL="C:/Users/Ram/Data/Angel/ANGEL_API_KEY=your_api_key.txt"
CRED_FILE_REMOTE="/root/angel_creds.txt"
SERVICE_NAME="algo-trading"

# SSH command shortcut
SSH="ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -i $SSH_KEY $VPS_USER@$VPS_IP"
SCP="scp -o ConnectTimeout=10 -o StrictHostKeyChecking=no -i $SSH_KEY"

echo "============================================"
echo "  ALGO TRADING - VULTR DEPLOYMENT"
echo "  VPS: $VPS_IP (Mumbai)"
echo "============================================"

# --- LOGS MODE ---
if [ "$1" = "logs" ]; then
    echo "Streaming live logs..."
    $SSH "journalctl -u $SERVICE_NAME -f --no-pager"
    exit 0
fi

# --- STATUS MODE ---
if [ "$1" = "status" ]; then
    echo ""
    $SSH "systemctl status $SERVICE_NAME --no-pager && echo '' && echo '--- Last 20 log lines ---' && journalctl -u $SERVICE_NAME -n 20 --no-pager"
    exit 0
fi

# --- SSH MODE ---
if [ "$1" = "ssh" ]; then
    $SSH
    exit 0
fi

# --- STOP MODE ---
if [ "$1" = "stop" ]; then
    echo "Stopping trading service..."
    $SSH "systemctl stop $SERVICE_NAME"
    echo "Service stopped."
    exit 0
fi

# --- START MODE ---
if [ "$1" = "start" ]; then
    echo "Starting trading service..."
    $SSH "systemctl start $SERVICE_NAME"
    sleep 2
    $SSH "systemctl status $SERVICE_NAME --no-pager"
    exit 0
fi

# --- RESTART MODE ---
if [ "$1" = "restart" ]; then
    echo "Restarting trading service..."
    $SSH "systemctl restart $SERVICE_NAME"
    sleep 2
    $SSH "systemctl status $SERVICE_NAME --no-pager"
    exit 0
fi

# --- CREDS MODE ---
if [ "$1" = "creds" ]; then
    echo "Uploading Angel API credentials..."
    if [ -f "$CRED_FILE_LOCAL" ]; then
        $SCP "$CRED_FILE_LOCAL" $VPS_USER@$VPS_IP:$CRED_FILE_REMOTE
        echo "Credentials uploaded. Restarting service..."
        $SSH "systemctl restart $SERVICE_NAME"
        echo "Done!"
    else
        echo "ERROR: Credential file not found at $CRED_FILE_LOCAL"
    fi
    exit 0
fi

# --- DASHBOARD MODE ---
if [ "$1" = "dashboard" ]; then
    echo "Checking dashboard..."
    $SSH "systemctl status algo-dashboard --no-pager 2>/dev/null || echo 'Dashboard service not found'"
    exit 0
fi

# --- STOCK BOT MODES ---
STOCK_SERVICE="algo-stock-trading"

if [ "$1" = "stock-logs" ]; then
    echo "Streaming stock bot logs..."
    $SSH "journalctl -u $STOCK_SERVICE -f --no-pager"
    exit 0
fi

if [ "$1" = "stock-status" ]; then
    echo ""
    $SSH "systemctl status $STOCK_SERVICE --no-pager && echo '' && echo '--- Last 20 log lines ---' && journalctl -u $STOCK_SERVICE -n 20 --no-pager"
    exit 0
fi

if [ "$1" = "stock-stop" ]; then
    echo "Stopping stock trading service..."
    $SSH "systemctl stop $STOCK_SERVICE"
    echo "Stock service stopped."
    exit 0
fi

if [ "$1" = "stock-start" ]; then
    echo "Starting stock trading service..."
    $SSH "systemctl start $STOCK_SERVICE"
    sleep 2
    $SSH "systemctl status $STOCK_SERVICE --no-pager"
    exit 0
fi

if [ "$1" = "stock-restart" ]; then
    echo "Restarting stock trading service..."
    $SSH "systemctl restart $STOCK_SERVICE"
    sleep 2
    $SSH "systemctl status $STOCK_SERVICE --no-pager"
    exit 0
fi

# --- OI BOT MODES ---
OI_SERVICE="algo-oi-trading"

if [ "$1" = "oi-logs" ]; then
    echo "Streaming OI bot logs..."
    $SSH "journalctl -u $OI_SERVICE -f --no-pager"
    exit 0
fi

if [ "$1" = "oi-status" ]; then
    echo ""
    $SSH "systemctl status $OI_SERVICE --no-pager && echo '' && echo '--- Last 20 log lines ---' && journalctl -u $OI_SERVICE -n 20 --no-pager"
    exit 0
fi

if [ "$1" = "oi-stop" ]; then
    echo "Stopping OI trading service..."
    $SSH "systemctl stop $OI_SERVICE"
    echo "OI service stopped."
    exit 0
fi

if [ "$1" = "oi-start" ]; then
    echo "Starting OI trading service..."
    $SSH "systemctl start $OI_SERVICE"
    sleep 2
    $SSH "systemctl status $OI_SERVICE --no-pager"
    exit 0
fi

if [ "$1" = "oi-restart" ]; then
    echo "Restarting OI trading service..."
    $SSH "systemctl restart $OI_SERVICE"
    sleep 2
    $SSH "systemctl status $OI_SERVICE --no-pager"
    exit 0
fi

if [ "$1" = "all-status" ]; then
    echo ""
    echo "=== Equity + Commodity Bot ==="
    $SSH "systemctl status $SERVICE_NAME --no-pager" 2>/dev/null || true
    echo ""
    echo "=== Stock Options Bot ==="
    $SSH "systemctl status $STOCK_SERVICE --no-pager" 2>/dev/null || true
    echo ""
    echo "=== OI Strategies Bot ==="
    $SSH "systemctl status $OI_SERVICE --no-pager" 2>/dev/null || true
    echo ""
    echo "=== Dashboard ==="
    $SSH "systemctl status algo-dashboard --no-pager" 2>/dev/null || true
    exit 0
fi

# --- DEFAULT: DEPLOY/UPDATE ---
echo ""
echo "Step 1: Syncing code to VPS..."

# Use rsync if available, otherwise scp
if command -v rsync &> /dev/null; then
    rsync -avz --progress \
        -e "ssh -o StrictHostKeyChecking=no -i $SSH_KEY" \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='logs/' \
        --exclude='paper_trades/' \
        --exclude='paper_trades_commodity/' \
        --exclude='paper_trades_crypto/' \
        --exclude='paper_trades_oi/' \
        --exclude='stock_paper_trades/' \
        --exclude='reports/' \
        --exclude='historical_data/' \
        --exclude='node_modules/' \
        --exclude='.env' \
        "$LOCAL_DIR/" $VPS_USER@$VPS_IP:$REMOTE_DIR/
else
    echo "  rsync not found, using scp (slower)..."
    $SCP -r "$LOCAL_DIR/" $VPS_USER@$VPS_IP:$REMOTE_DIR/
fi

echo ""
echo "Step 2: Installing dependencies on VPS..."
$SSH "cd $REMOTE_DIR && pip3 install -r requirements.txt -q 2>&1 | tail -3"

echo ""
echo "Step 3: Uploading Angel credentials..."
if [ -f "$CRED_FILE_LOCAL" ]; then
    $SCP "$CRED_FILE_LOCAL" $VPS_USER@$VPS_IP:$CRED_FILE_REMOTE
    echo "  Credentials uploaded to $CRED_FILE_REMOTE"
else
    echo "  WARNING: Credential file not found at $CRED_FILE_LOCAL"
fi

echo ""
echo "Step 4: Creating data directories..."
$SSH "mkdir -p $REMOTE_DIR/paper_trades_oi $REMOTE_DIR/data/oi_history $REMOTE_DIR/locks"

echo ""
echo "Step 5: Restarting ALL trading services..."
$SSH "systemctl restart $SERVICE_NAME && systemctl restart algo-stock-trading && systemctl restart algo-oi-trading 2>/dev/null; systemctl restart algo-dashboard"
sleep 3

echo ""
echo "Step 6: Verifying..."
echo "--- Equity + Commodity ---"
$SSH "systemctl status $SERVICE_NAME --no-pager" 2>/dev/null || true
echo ""
echo "--- Stock Options ---"
$SSH "systemctl status algo-stock-trading --no-pager" 2>/dev/null || true
echo ""
echo "--- OI Strategies ---"
$SSH "systemctl status algo-oi-trading --no-pager" 2>/dev/null || true
echo ""
echo "--- Dashboard ---"
$SSH "systemctl status algo-dashboard --no-pager" 2>/dev/null || true

echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE!"
echo "============================================"
echo ""
echo "  Equity/Commodity:"
echo "    Logs:    ./deploy_vultr.sh logs"
echo "    Status:  ./deploy_vultr.sh status"
echo "    Stop:    ./deploy_vultr.sh stop"
echo ""
echo "  Stock Options:"
echo "    Logs:    ./deploy_vultr.sh stock-logs"
echo "    Status:  ./deploy_vultr.sh stock-status"
echo "    Stop:    ./deploy_vultr.sh stock-stop"
echo ""
echo "  OI Strategies:"
echo "    Logs:    ./deploy_vultr.sh oi-logs"
echo "    Status:  ./deploy_vultr.sh oi-status"
echo "    Stop:    ./deploy_vultr.sh oi-stop"
echo ""
echo "  All:"
echo "    SSH:     ./deploy_vultr.sh ssh"
echo "    Status:  ./deploy_vultr.sh all-status"
echo "============================================"
