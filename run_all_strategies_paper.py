import threading
import time
import logging
import requests
from dotenv import load_dotenv

load_dotenv()

# === YOUR TELEGRAM BOT ===
TELEGRAM_TOKEN = "8426384062:AAGJKO8y7ijbSMdbaFnxkfOeLFlnQZWc8oI"
TELEGRAM_CHAT_ID = "1722559857"

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("all_strategies_paper.log"), logging.StreamHandler()]
)
logger = logging.getLogger("AllPaper")

broker = Angel_broker()
order_tracker = OrderTracker()

from strategies.survivor_strategy import SurvivorStrategy
from strategies.survivor_v2_oi_gamma import SurvivorV2Strategy
from strategies.gamma_blast_strategy import GammaBlastStrategy
from strategies.pcr_vwap_strategy import PCRVWAPStrategy
from strategies.cpr_strategy import CPRStrategy
from strategies.ghost_zone_strategy import GhostZoneStrategy
from strategies.wave_strategy import WaveStrategy

strategies_list = [
    ("SurvivorV2_OI_Gamma", SurvivorV2Strategy, 80000),
    ("GammaBlast", GammaBlastStrategy, 50000),
    ("PCR_Nitin", PCRVWAPStrategy, 40000),
    ("CPR", CPRStrategy, 30000),
    ("Wave", WaveStrategy, 30000),
    ("GhostZone", GhostZoneStrategy, 30000),
    ("ClassicSurvivor", SurvivorStrategy, 40000),
]

def run_strategy(name, StrategyClass, capital):
    try:
        config = {"capital": capital, "mode": "paper"}
        strat = StrategyClass(broker, config, order_tracker)
        logger.info(f"✅ Started {name} | ₹{capital:,}")
        send_telegram(f"🚀 {name} started on paper (₹{capital:,})")
        while True:
            time.sleep(60)
            logger.info(f"{name} running...")
    except Exception as e:
        logger.error(f"{name} crashed: {e}", exc_info=True)
        send_telegram(f"❌ {name} crashed: {e}")

threads = []
for name, cls, cap in strategies_list:
    t = threading.Thread(target=run_strategy, args=(name, cls, cap), daemon=True, name=name)
    t.start()
    threads.append(t)

logger.info("🎉 ALL 7 STRATEGIES RUNNING IN PAPER MODE")
send_telegram("🎉 All 7 strategies started in paper trading on GCP!\nNifty + BankNifty + SENSEX active.\nWinner will be picked in 5-10 days.")

while True:
    time.sleep(3600)