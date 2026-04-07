"""
MC-OPTIMAL PAPER TRADING LAUNCHER
===================================
Runs the same paper trading system but with MC-optimal parameters:
  Choppy Shield: 20% (vs current 15%)
  DCI Gate:      >=48 (vs current 40)
  Wave FLAT:     Block (new)

Uses PARAM_SET=mc_optimal environment variable to switch parameters.
Logs to separate files: unified_paper_mc_YYYYMMDD.log
Portfolio stored separately: paper_portfolio_mc.json

Usage:
    python run_paper_trade_mc.py                # Run MC-optimal version
    python run_paper_trade_mc.py --status       # Show MC portfolio
    python run_paper_trade_mc.py --reset        # Reset MC portfolio
"""

import os
import sys

# Set MC-optimal params BEFORE importing anything
os.environ['PARAM_SET'] = 'mc_optimal'
os.environ['PORTFOLIO_SUFFIX'] = '_mc'  # Separate portfolio file
os.environ['TRADING_INSTANCE'] = 'vultr_mc'  # Separate PID lock

# Override log file name
import logging
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_DIR, exist_ok=True)

today_str = datetime.now().strftime('%Y%m%d')

# Clear any existing handlers
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(threadName)s] [MC] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, f'unified_paper_mc_{today_str}.log'), encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('MCPaperTrader')
logger.info(f"MC-OPTIMAL Paper Trader starting — PARAM_SET=mc_optimal")
logger.info(f"Params: Choppy=20%, DCI>=48, Wave FLAT block=ON")

# Now import and run the main paper trade launcher
import run_paper_trade

# Override lock file to avoid conflict with main instance
run_paper_trade.LOCK_FILE = os.path.join(run_paper_trade.LOCK_DIR, 'trading_mc.pid')

if __name__ == '__main__':
    run_paper_trade.main()
