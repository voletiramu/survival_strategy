# AlgoTrading Strategy Guide — Simple Language
### For Ram & Son — Working Together on This Project

---

## WHAT DOES OUR BOT DO?

Our bot watches the Indian stock market (NIFTY, BANKNIFTY, SENSEX) and buys **options** (CE = Call = betting price goes UP, PE = Put = betting price goes DOWN). It doesn't buy actual stocks — it buys the RIGHT to buy/sell at a specific price.

**Think of it like this:**
- NIFTY is at 23,000. We buy a "23,000 CE" option for Rs 150.
- If NIFTY goes up to 23,200, our option might become Rs 250 (+67% profit).
- If NIFTY goes down, our option loses value. We set a stop-loss to limit damage.

**The bot's job:** Find the RIGHT moment to buy, and the RIGHT moment to sell.

---

## THE 3 BOTS

| Bot | What it Trades | Capital | Market Hours |
|-----|---------------|---------|-------------|
| **Equity Bot** | NIFTY, BANKNIFTY, SENSEX options | Rs 3,00,000 | 9:15 AM - 3:30 PM |
| **Commodity Bot** | Gold, Silver, Crude Oil options | Rs 3,00,000 | 9:00 AM - 11:30 PM |
| **Stock Bot** | Individual stock options (SBIN, RELIANCE, etc.) | Rs 2,00,000 | 9:15 AM - 3:30 PM |

---

## HOW THE BOT RUNS (Every 1 Second)

```
Every 1 second, the bot does this:
  1. CHECK PRICES  — What is NIFTY/BANKNIFTY/SENSEX at right now?
  2. RUN STRATEGIES — Do any of our 6 strategies say "BUY NOW"?
  3. FILTER         — Is the signal strong enough? Do we have capital? Any cooldowns?
  4. EXECUTE         — If yes, buy the option (paper trade, not real money yet)
  5. WATCH EXITS     — For trades we already hold, should we sell?
  6. LOG             — Write everything to log files for analysis
```

---

## THE 6 ACTIVE STRATEGIES (Equity Bot)

### Strategy 1: CPR (Central Pivot Range) — 25% of Capital

**What is CPR?**
Every night, we calculate 3 magic lines from yesterday's High, Low, Close:
- **TC** (Top Central) — resistance line above
- **Pivot** — the middle line
- **BC** (Bottom Central) — support line below

**The Idea (Gomathi Shankar method):**
- If today's price breaks ABOVE TC → market is strong → BUY CE (bet UP)
- If today's price breaks BELOW BC → market is weak → BUY PE (bet DOWN)
- If the range between TC and BC is very wide → mean reversion (price comes back to middle)

**Simple Example:**
```
Yesterday: NIFTY High=23,200, Low=22,800, Close=23,050

TC = 23,167  (calculated)
BC = 22,883  (calculated)

Today at 10:30 AM: NIFTY crosses above 23,167
Bot thinks: "Breakout! Going up!" → Buys NIFTY 23,200 CE at Rs 140
```

**When to Exit:**
- Target: +40% profit (Rs 140 → Rs 196)
- Stop Loss: -50% (Rs 140 → Rs 70)
- Or: Trailing Stop Loss catches it

**Win Rate:** ~65% (solid and consistent)

---

### Strategy 2: Gamma Blast — 30% of Capital

**What is Gamma?**
Gamma is how FAST an option's price changes. Near expiry day (Thursday), gamma is HUGE — small moves in NIFTY cause massive swings in option price.

**The Idea:**
Look for days when the market was quiet/flat recently (coiled spring), then suddenly starts moving. The "blast" = the spring uncoiling.

**How it works:**
1. Check if recent range was tight (low volatility = coiled spring)
2. Today, check if price moved significantly from open (body > 0.15 x ATR)
3. Confirm direction with VWAP (price should be above VWAP for bullish, below for bearish)
4. Enter in the direction of the move

**Simple Example:**
```
Last 3 days: NIFTY moved only 100 points per day (tight range)
Today by 10:00 AM: NIFTY already moved +250 points from open
VWAP: NIFTY is above VWAP (confirming upward pressure)

Bot thinks: "Coil is releasing upward!" → Buys CE
```

**Why it works on expiry:**
On expiry Thursday, a Rs 50 option can become Rs 200 in 30 minutes because of gamma. The "blast" happens because all the option sellers (who sold cheap options) panic and buy back, pushing prices even higher.

**Win Rate:** ~91% in strong trends (Sharpe 4.42 — best risk-adjusted strategy)

---

### Strategy 3: Ghost Zone v7 — 15% of Capital

**The Idea (Manish Maheshwari method):**
Big institutions (mutual funds, FIIs) leave "footprints" in the chart. These footprints create invisible zones where price tends to bounce.

**How it works (Family Analogy):**
1. **Grandfather (Candlestick)** — Look at 4-hour candles for the last 5 days. Find "institutional candles" = pin bars (long wicks), expansion bars (big moves), or dojis (indecision).

2. **Father (Volume)** — The institutional candle MUST have 3x more volume than average. High volume = big players were active at that price level.

3. **Mother (OI = Open Interest)** — Check if institutions built positions at that zone (high OI concentration near the zone).

4. **Entry** — When price comes BACK to touch that zone (50% of zone = sweet spot), enter the trade.

**Simple Example:**
```
3 days ago at 22,800, there was a huge pin bar candle with 3x volume
This created a "demand zone" at 22,750 - 22,850

Today: NIFTY drops to 22,800 (touching the zone)
Bot checks: OI data shows institutions have positions here
Bot thinks: "Institutions will defend this zone!" → Buys CE

SL: Below the zone (22,740)
```

**Win Rate:** ~70% (high win rate but lower Sharpe than Gamma Blast)

---

### Strategy 4: PCR + VWAP — 10% of Capital

**What is PCR?**
PCR = Put-Call Ratio = Total Put OI / Total Call OI
- PCR > 1.0 = more puts than calls = market is hedged = BULLISH
- PCR < 0.7 = more calls than puts = market is greedy = BEARISH

**What is VWAP?**
VWAP = Volume Weighted Average Price = the "fair price" for today
- Price above VWAP = bullish (buyers in control)
- Price below VWAP = bearish (sellers in control)

**The Idea:**
Combine institutional flow (PCR shift) with trend confirmation (VWAP).

**How it works:**
1. PCR shift = how much PCR changed TODAY (e.g., from 0.80 to 1.10 = +0.30 shift = bullish)
2. Price must be ABOVE VWAP (confirming the bullish trend)
3. Shift must be >= +0.03 (meaningful, not noise)
4. PCR not extreme (< 1.5, otherwise reversal risk)

**Important: PE entries are DISABLED** — bearish PCR shifts only work 28-32% of the time, so we only trade bullish signals.

**Simple Example:**
```
Morning: PCR was 0.85
Now (11:00 AM): PCR is 1.15 (shift = +0.30 — very bullish)
NIFTY: Above VWAP
Price is above today's open

Bot thinks: "Institutions are buying puts = hedging = bullish!" → Buys CE
```

**Win Rate:** Varies — needs good OI data quality

---

### Strategy 5: Trend Rider — 20% of Capital

**The Idea:**
Some days are "trend days" — the market moves in ONE direction all day. These are the easiest days to make money. But you need to CONFIRM the trend first (not jump in at open).

**How it works:**
1. Wait until 10:15 AM (let the first hour confirm the direction)
2. Check: Did the market gap up/down at open? (max 1.5% gap — too big = reversal risk)
3. Check: Is the first hour's move strong enough? (> 0.5% in one direction)
4. Score the signal out of 10 (gap + momentum + VWAP + PCR + previous day's trend)
5. If score >= 5 out of 10 → Enter in the trend direction
6. Must enter by 10:30 AM (entry window is only 15 minutes)
7. Maximum 1 Trend Rider trade per symbol per day

**Simple Example:**
```
Today: NIFTY opened at 23,000 (no gap — good)
By 10:15 AM: NIFTY is at 23,180 (+0.78% — strong upward move)
VWAP: Price above VWAP (confirming)
PCR: Shifted bullish (+0.05)

Score: 7/10
Bot thinks: "Strong trend day confirmed!" → Buys CE with wider stop loss (35%)
```

**Why wider SL?** Trend days have pullbacks. A tight SL would kick you out on a dip before the trend resumes. We give it room to breathe (SL = 35% of entry premium, but trail after 35% gain).

**Win Rate (Backtest):** 66.7% WR, PF 5.56

---

### Strategy 6: Liquidity Sweep — 15% of Capital

**The Idea:**
Big institutions sometimes deliberately push the price to hit everyone's stop losses, grab those shares cheaply, then reverse the move. This is called a "liquidity sweep" or "stop hunt."

**How it works:**
1. **Detect the sweep** — Price spikes beyond a key level (previous high/low, round number) and then QUICKLY reverses back. This looks like a long wick candle.
2. **Confirm the reversal** — The reversal must be strong (the "spike reversal ratio" measures how much of the spike was reversed).
3. **Acceleration check** — Is the reversal speeding up? (not just a slow drift back)
4. **Enter opposite to the sweep direction** — If they swept stops above resistance, price is going DOWN. If they swept below support, price is going UP.

**Simple Example:**
```
BANKNIFTY at 51,000 with resistance at 51,200

10:30 AM: Suddenly spikes to 51,350 (above 51,200 — swept stops of people who sold at resistance)
10:32 AM: Drops back to 51,150 (reversed 80% of the spike)
10:33 AM: Still falling — acceleration confirmed

Bot thinks: "Stop hunt completed, smart money is shorting!" → Buys PE
```

**Status:** Active in equity bot but DISABLED in commodity bot (was causing crashes)

---

## HALTED & DISABLED STRATEGIES

### Survivor (HALTED — Needs More Capital)
**What it does:** SELLS options instead of buying them. You collect premium upfront and profit if the option expires worthless.
**Why halted:** Selling options requires huge margin (Rs 1,00,000+ per lot). Our Rs 3L capital can't support it safely.
**Potential:** Highest absolute PnL in backtests (Rs 36.5M annually, 91% WR) — but only with Rs 10L+ capital.

### Ghost Zone — Commodity (DISABLED)
Weak zone detection on commodities — consistently losing. Needs different parameters for Gold/Silver/Crude.

### Liquidity Sweep — Commodity (DISABLED)
Was causing scan crashes in commodity bot. Needs code fixes.

### PCR+VWAP PE entries (DISABLED)
Bearish PCR shifts only 28-32% accurate. Only bullish (CE) entries work.

---

## BACKTEST-ONLY STRATEGIES (Not Live Yet)

These exist in code for research but aren't connected to live trading:

| Strategy | File | What it Does |
|----------|------|-------------|
| **ORB** (Opening Range Breakout) | `orb_strategy.py` | Trades breakout of first 15-min range |
| **Supertrend** | `supertrend_strategy.py` | Follows supertrend indicator (ATR-based trailing line) |
| **MACD + ADX** | `macd_adx_strategy.py` | MACD crossover + ADX strength confirmation |
| **Wave** | `wave_strategy.py` | Grid-based entries at fixed intervals |
| **OI Wall Bouncer** | `oi_strategies.py` | Bounces off high OI concentration walls |
| **Max Pain Magnet** | `oi_strategies.py` | Trades toward max pain level |
| **VWAP Bounce** | `oi_strategies.py` | Mean reversion at VWAP with OI confirmation |

---

## THE EXIT SYSTEM (How We Sell)

Knowing WHEN to buy is only half the battle. Knowing WHEN to sell is where the real money is made. Our bot has **16 layers** of exit logic, but the most important ones are:

### 1. Trailing Stop Loss (TSL) — THE MOST IMPORTANT EXIT

Think of TSL like a loyal dog on a leash. As your trade goes UP, the dog (stop loss) follows UP. But the dog never goes DOWN.

```
You buy at Rs 100

Phase 0 (Micro Trail — after 5% gain):
  Premium hits Rs 105 → SL set at Rs 58 (keep 55% of peak)

Phase 1 (Breakeven Lock — after 10% gain):
  Premium hits Rs 110 → SL locked at Rs 100 (GUARANTEED no loss!)

Phase 2 (Standard Trail — after 20% gain):
  Premium hits Rs 120 → SL at Rs 96 (trail 20% below peak)
  Premium hits Rs 140 → SL at Rs 112 (SL moved UP with price)

Phase 3 (Tight Trail — after 35% gain):
  Premium hits Rs 135 → SL at Rs 119 (trail only 12% below peak — LOCK PROFITS)
```

**Key insight:** The SL only moves UP, never DOWN. So even if price drops, you keep most of your profit.

### 2. Circuit Breaker
If we lose Rs 30,000 in a single day → CLOSE EVERYTHING. Stop trading. Protect capital.

### 3. EOD Force Close
At 3:20 PM (10 minutes before market close) → close all positions. Zero overnight risk.

### 4. Grace Period
After entering a trade, wait 3 minutes before any exit check. Prevents panic selling on initial noise.

### 5. Target Extension
When our target is hit (say +40%), instead of immediately selling:
→ Extend the target by 20% more
→ Tighten the TSL to 15% below peak
→ Let the winner run (up to 5 extensions)

---

## DATA FLOW — WHERE DOES OUR DATA COME FROM?

```
SOURCE PRIORITY (for option prices):

1st: Zerodha Kite API    ← PRIMARY (what we pay Rs 2,000/month for)
2nd: TrueData WebSocket  ← Backup (trial account)
3rd: NSE/BSE Pipeline    ← Direct from exchanges (3-min delay)
4th: Angel One API       ← Last resort (slowest)

For SPOT prices (NIFTY value):
→ WebSocket feed (instant, free, no API calls)

For indicators (ATR, RSI, CPR):
→ Angel historical OHLC data (downloaded once, cached 60 seconds)
```

---

## RISK MANAGEMENT — HOW WE PROTECT CAPITAL

| Rule | Value | Why |
|------|-------|-----|
| Max per trade | Rs 75,000 (25% of segment) | No single trade can wipe us out |
| Daily loss limit | Rs 30,000 (circuit breaker) | Stop digging when in a hole |
| Max positions per symbol | 3 | Don't pile into one losing bet |
| Max trades per day | 15 | Don't overtrade |
| No trades before 9:30 AM | First 15 min = wild, inflated premiums | Let market settle |
| No new trades after 2:30 PM | Need 50+ minutes for trade to develop | Avoid EOD squeeze |
| VIX > 35 → BLOCK all trades | Too volatile, unpredictable | Protect capital |
| VIX < 11 → BLOCK all trades | No movement, waste brokerage | Save costs |
| Drawdown > 5% → Single lot only | When losing, reduce size | Survive to fight another day |
| Re-entry cooldown | 10 min after exit | Prevent revenge trading |

---

## WHAT THE NUMBERS MEAN

| Term | Simple Explanation |
|------|-------------------|
| **WR (Win Rate)** | Out of 100 trades, how many were profitable? WR 65% = 65 winners, 35 losers |
| **PF (Profit Factor)** | Total wins / Total losses. PF 2.0 = made Rs 2 for every Rs 1 lost |
| **Sharpe Ratio** | Return per unit of risk. > 2.0 is great, > 3.0 is exceptional |
| **ATR** | Average True Range — how much NIFTY moves per day (in points) |
| **OI (Open Interest)** | How many option contracts are "alive". High OI = big players present |
| **IV (Implied Volatility)** | Market's expectation of future movement. High IV = expensive options |
| **Delta** | How much option price changes for Rs 1 change in NIFTY. Delta 0.5 = Rs 0.50 per point |
| **Gamma** | How fast delta changes. High gamma near expiry = explosive option moves |
| **Theta** | Time decay — how much option loses per day just from time passing |
| **VWAP** | Today's "fair price" weighted by volume. Above VWAP = bullish |
| **PCR** | Put-Call Ratio. > 1.0 = bearish hedging (actually bullish for market) |
| **DTE** | Days To Expiry. Lower DTE = higher gamma, more theta decay |
| **TSL** | Trailing Stop Loss — moves up as trade profits, never goes down |
| **CE** | Call option — profits when price goes UP |
| **PE** | Put option — profits when price goes DOWN |

---

## PROJECT FILE STRUCTURE

```
D:/AlgoTrading/
├── algo_trading/              ← ALL CODE (synced with VPS)
│   ├── paper_trader.py        ← Main equity bot (5900 lines)
│   ├── commodity_paper_trader.py ← Commodity bot
│   ├── stock_paper_trader.py  ← Stock F&O bot
│   ├── run_paper_trade.py     ← Launcher (runs equity + commodity together)
│   ├── dashboard.py           ← Web dashboard (http://65.20.69.104:5000)
│   ├── trade_notifier.py      ← Telegram alerts
│   ├── ws_feed.py             ← WebSocket real-time prices
│   ├── zerodha_feed.py        ← Zerodha API (Source 1)
│   ├── truedata_feed.py       ← TrueData API (Source 2)
│   ├── market_data_pipeline.py ← NSE/BSE direct feed (Source 3)
│   ├── market_regime.py       ← Trend/Sideways/Flat detection
│   ├── market_calculus.py     ← VWAP + momentum engine
│   ├── trade_intelligence.py  ← P(win) estimator
│   ├── market_holidays.py     ← NSE/MCX holiday calendar
│   ├── stock_fno_config.py    ← Stock lot sizes and config
│   ├── templates/
│   │   └── dashboard.html     ← Dashboard UI
│   └── static/                ← Icons, PWA files
│
├── backtest/                  ← Backtest scripts
├── data/                      ← Historical data (CSVs)
├── config/                    ← API keys (NEVER commit to GitHub)
├── Paper_Trading_Algorithm.md ← Full technical algorithm doc
└── Strategy_Guide_Simple.md   ← THIS FILE (simple explanation)
```

---

## VPS (Cloud Server) DETAILS

```
Server:    Vultr Ubuntu VPS
IP:        65.20.69.104
SSH:       ssh -i "C:/Users/Ram/.ssh/id_rsa_vultr" root@65.20.69.104
Code:      /root/algo_trading/
Logs:      /root/algo_trading/logs/
Dashboard: http://65.20.69.104:5000

Services:
  algo-trading.service     → Equity + Commodity bot
  algo-stock-trading.service → Stock F&O bot

Commands:
  systemctl restart algo-trading    ← Restart bots
  systemctl status algo-trading     ← Check if running
  journalctl -u algo-trading -f     ← Watch live logs

GitHub: https://github.com/voletiramu/survival_strategy
Branch: feature/angel-data-backtest
```

---

## WHAT WE'RE WORKING ON (Current Challenges)

### The Consistency Problem
Our backtesting found that 95% of profits come from just 3-4 "jackpot" trades on crash days (Mar 11-12, 2026). Remove those 3-4 trades → we're in LOSS.

**What we need:** A strategy that makes small, consistent profits on NORMAL days, not one that depends on rare market crashes.

### Possible Solutions Being Explored
1. **Momentum entry** — Only enter when option premium is already rising (confirm momentum before committing)
2. **Quick scalp** — Take 2-3% profit quickly instead of waiting for 20%+ home runs
3. **Flat exit** — If premium doesn't move for 3-5 minutes, exit immediately (avoid timeout trades that just pay brokerage)
4. **Tighter SL** — Use 2-3% SL instead of 10-15% (lose less per trade)

---

## HOW TO CONTRIBUTE (For Son)

### Quick Start
1. Clone: `git clone https://github.com/voletiramu/survival_strategy.git`
2. Look at `paper_trader.py` — search for `check_cpr_signals` to see a strategy
3. Each strategy is a function that takes (symbol, spot_price, indicators) and returns signals
4. Signals are dicts: `{type: 'BUY_CE', strike: 23000, premium: 150, quality_score: 75, ...}`

### Key Places to Edit
- **Add a new strategy:** Write a `check_your_strategy_signals()` function, add to `scan_all_strategies()`
- **Change exit logic:** Edit `check_exits()` in paper_trader.py
- **Backtest:** Use files in `backtest/` directory with real signal data from `paper_trades/signals_*.csv`
- **Dashboard:** Edit `templates/dashboard.html` (single-file HTML+CSS+JS)

### Python Libraries Used
`scipy, numpy, pandas, matplotlib, plotly, kiteconnect (Zerodha), SmartApi (Angel), truedata-ws, TA-Lib`

---

*Last updated: 2026-03-21 | System version: v15.0 (tick-by-tick scanning)*
