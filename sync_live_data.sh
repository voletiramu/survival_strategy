#!/bin/bash
# sync_live_data.sh — Pull live market data from VPS to local machine
#
# Usage:
#   ./sync_live_data.sh              # Sync all dates
#   ./sync_live_data.sh 2026-03-11   # Sync specific date
#
# Data is synced from VPS (65.20.69.104) to local C:/Users/Ram/Data/algo_trading/data/live/
#
# v10.2e: Created for historical backtesting data collection

VPS_HOST="root@65.20.69.104"
VPS_KEY="$HOME/.ssh/id_rsa_vultr"
VPS_DATA_DIR="/root/algo_trading/data/live/"
LOCAL_DATA_DIR="/c/Users/Ram/Data/algo_trading/data/live/"

# Create local directory if it doesn't exist
mkdir -p "$LOCAL_DATA_DIR"

if [ -n "$1" ]; then
    # Sync specific date
    echo "Syncing live data for $1 from VPS..."
    rsync -avz --progress \
        -e "ssh -i $VPS_KEY -o StrictHostKeyChecking=no" \
        "${VPS_HOST}:${VPS_DATA_DIR}$1/" \
        "${LOCAL_DATA_DIR}$1/"
else
    # Sync all dates
    echo "Syncing ALL live data from VPS..."
    rsync -avz --progress \
        -e "ssh -i $VPS_KEY -o StrictHostKeyChecking=no" \
        "${VPS_HOST}:${VPS_DATA_DIR}" \
        "${LOCAL_DATA_DIR}"
fi

echo ""
echo "=== Sync Complete ==="
echo "Local data directory: $LOCAL_DATA_DIR"

# Show summary
if [ -d "$LOCAL_DATA_DIR" ]; then
    echo ""
    echo "Available dates:"
    ls -d "${LOCAL_DATA_DIR}"*/ 2>/dev/null | while read dir; do
        date_name=$(basename "$dir")
        file_count=$(ls "$dir" | wc -l)
        total_size=$(du -sh "$dir" | cut -f1)
        echo "  $date_name: $file_count files, $total_size"
    done
fi
