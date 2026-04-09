# PAPER TRADING SYSTEM — COMPLETE ALGORITHM (v7.6 / v15.0)
## Automated Options Trading for Indian Markets (NSE/BSE/MCX)

---

## TABLE OF CONTENTS

1. [System Architecture](#1-system-architecture)
2. [Boot Sequence & Initialization](#2-boot-sequence--initialization)
3. [Main Trading Loop](#3-main-trading-loop)
4. [Data Collection Layer](#4-data-collection-layer)
5. [Indicator Computation](#5-indicator-computation)
6. [Strategy 1: CPR (Central Pivot Range) — 30%](#6-strategy-1-cpr-central-pivot-range--30)
7. [Strategy 2: Gamma Blast — 40%](#7-strategy-2-gamma-blast--40)
8. [Strategy 3: Ghost Zone v7 — 20%](#8-strategy-3-ghost-zone-v7--20)
9. [Strategy 4: PCR+VWAP — 10%](#9-strategy-4-pcrvwap--10)
10. [Strategy 5: Trend Rider — Bonus](#10-strategy-5-trend-rider--bonus)
11. [Signal Scoring & Quality Filter](#11-signal-scoring--quality-filter)
12. [Pre-Execution Filters](#12-pre-execution-filters)
13. [Position Sizing & Capital Management](#13-position-sizing--capital-management)
14. [Exit Engine (16 Layers)](#14-exit-engine-16-layers)
15. [Trailing Stop Loss (TSL) — Core Exit Mechanism](#15-trailing-stop-loss-tsl--core-exit-mechanism)
16. [Market Regime Detection](#16-market-regime-detection)
17. [Risk Management](#17-risk-management)
18. [Data Sources & Fallback Chain](#18-data-sources--fallback-chain)
19. [Commodity Bot Differences](#19-commodity-bot-differences)
20. [Stock F&O Bot Differences](#20-stock-fo-bot-differences)
21. [Deployment & Infrastructure](#21-deployment--infrastructure)
22. [Known Bugs & Fixes](#22-known-bugs--fixes)
23. [Cost Model](#23-cost-model)
24. [Key Constants Reference](#24-key-constants-reference)

---

## 1. SYSTEM ARCHITECTURE

### 4-Class Design (per bot)

```
┌─────────────────────────────────────────────────────┐
│                   PaperTrader                       │
│  (Main Orchestrator — scan, execute, exit, report)  │
├─────────────────────────────────────────────────────┤
│                  StrategyEngine                     │
│  (Signal generation — CPR, Gamma, Ghost, PCR+VWAP) │
├─────────────────────────────────────────────────────┤
│                 PaperPortfolio                      │
│  (Capital, positions, PnL, lot sizing, risk)        │
├─────────────────────────────────────────────────────┤
│                AngelConnection                      │
│  (Angel One SmartAPI — instruments, OHLC, fallback) │
│  NOTE: Angel is now Source 4 (last fallback only).  │
│  Primary data comes from Zerodha (Source 1).        │
└─────────────────────────────────────────────────────┘

Supporting Modules:
  ZerodhaFeed         → Zerodha Kite Connect — PRIMARY (Source 1) for option LTP + chains
  TrueDataFeed        → TrueData WebSocket (Source 2 fallback)
  MarketDataPipeline  → Real-time NSE/BSE option chains, VIX, PCR (Source 3)
  MarketRegime        → Trend/Sideways/Flat detection with hysteresis
  MarketCalculus      → Intraday VWAP + momentum direction analysis
  TradeIntelligence   → P(win) estimation, dynamic SL adjustment
  WebSocketFeed       → Real-time spot prices (WebSocket)
  TradeNotifier       → Telegram/Email alerts

Angel One is retained ONLY for:
  - Instrument master (contract list, tokens)
  - Historical daily OHLC (for ATR/RSI/CPR calculation)
  - Spot price fallback (when WebSocket is down)
  - Source 4 option LTP (last resort)
```

### 3 Independent Bots

| Bot | File | Symbols | Capital | Service |
|-----|-------|---------|---------|---------|
| Equity | `paper_trader.py` (5885 lines) | NIFTY, BANKNIFTY, SENSEX | Rs 3,00,000 | `algo-trading.service` |
| Commodity | `commodity_paper_trader.py` (4272 lines) | GOLDM, SILVERM, CRUDEOILM | Rs 3,00,000 | `algo-trading.service` |
| Stock F&O | `stock_paper_trader.py` (4402 lines) | Individual F&O stocks | Rs 2,00,000 | `algo-stock-trading.service` |

---

## 2. BOOT SEQUENCE & INITIALIZATION

```
Step 1: Parse CLI arguments (--status / --once / --interval / --offline / --reset)
Step 2: Create PaperTrader instance
        ├── AngelConnection() — API wrapper
        ├── PaperPortfolio() — loads saved state from paper_trades/portfolio_state.json
        ├── StrategyEngine(angel, portfolio) — indicator engine
        ├── ZerodhaFeed.connect() — Source 1 (primary option LTP)
        ├── TrueDataFeed.connect() — Source 2 (fallback option LTP)
        ├── MarketCalculus() — VWAP/momentum engine
        ├── TradeIntelligence() — dynamic P(win) tracker
        └── RegimeDetector() — trend/sideways detection
Step 3: Load historical OHLC data for NIFTY, BANKNIFTY, SENSEX
        └── Angel SmartAPI daily bars (auto-downloads if stale >1 day)
Step 4: Connect to Angel One API (try Historical mode, fallback Market mode)
Step 5: Load instrument master (all NSE/BSE/MCX contracts)
Step 6: Recover daily_trade_count from saved portfolio state (restart-safe)
Step 7: Enter main loop → run_continuous(interval_seconds=1)  ← v15: tick-by-tick
```

---

## 3. MAIN TRADING LOOP

```
run_continuous():
│
├── LOOP (while running):
│   ├── Check time: PRE_MARKET (09:00) <= now <= MARKET_CLOSE (15:30)?
│   │   ├── YES → run_once()
│   │   ├── now > MARKET_CLOSE → print_status(), save_daily_report(), BREAK
│   │   └── now < PRE_MARKET → sleep (max 5 min intervals)
│   └── sleep(interval_seconds)  ← v15: default 1 second (was 5 minutes)
│
└── run_once():
    ├── Step A: scan_all_strategies()    → generates signal list
    ├── Step B: execute_paper_signals()  → filters & executes signals
    ├── Step C: check_exits()            → multi-layer exit engine
    ├── Step D: portfolio.print_status() → log current state
    └── Step E: save_signals_log()       → append ALL signals to CSV
```

### Step A: scan_all_strategies()

```
For each symbol in [NIFTY, BANKNIFTY, SENSEX]:
  1. Get spot price (WebSocket → REST API → historical close)
  2. Feed spot to MarketCalculus (VWAP/momentum tracking)
  3. Update RegimeDetector with spot + VIX
  4. Get intraday OHLC
  5. Compute indicators (ATR, RSI, CPR, VWAP, IV, efficiency)
  6. Fetch real PCR (MarketDataPipeline → Angel API → proxy)
  7. Enrich with pipeline data (PCR shift, OI sentiment, premium sentiment)
  8. Run all 4 strategy checks:
     ├── engine.check_cpr_signals(symbol, spot, indicators)
     ├── engine.check_gamma_signals(symbol, spot, indicators)
     ├── engine.check_ghost_zone_signals(symbol, spot, indicators)
     └── engine.check_pcr_vwap_signals(symbol, spot, indicators)
  9. Collect all signals into master list
```

### Step B: execute_paper_signals(signals)

```
PRE-CHECKS:
  ├── Market open? No → skip all
  ├── Before 09:30? → EARLY_MARKET_BLOCK (inflated premiums, OI spikes)
  ├── After 14:30? → LATE_ENTRY_BLOCK (need 50+ min to develop)
  └── daily_trade_count >= 15? → MAX_TRADES_REACHED

CONFLICT FILTER:
  If same symbol has both CE and PE signals in this batch:
    → Keep only the higher-scored direction
    → Suppress all signals for weaker direction

VIX HARD GATE:
  ├── VIX > 35 → block ALL entries (too volatile)
  └── VIX < 11 → block ALL entries (no movement expected)

FOR EACH SIGNAL:
  ├── Duplicate check (same strategy + symbol + direction already held?)
  ├── Re-entry escalation (2nd entry needs score 65, 3rd needs 80, 4th needs 95)
  ├── Direction cap (max 5 same-direction trades per symbol per day)
  ├── Cross-strategy dedup (another strategy already holds same symbol+direction?)
  ├── Conflicting position check (no BUY_CE + SELL_CE on same strike)
  ├── Direction flip logic (close losing opposite position if new signal score >= 70)
  ├── Re-entry cooldown (10 min after any exit, 15 min after DIRECTION_FLIP)
  ├── Ghost Zone cooldown (30 min after GTZ loss, same direction)
  ├── Choppy day filter (block after 11:00 if efficiency < 15%)
  ├── Score threshold check (signal score >= escalated_min_score?)
  ├── Get real option premium (4-source fallback chain)
  ├── Premium quality filter:
  │   ├── BUY: premium >= Rs 15 (NIFTY) / Rs 40 (BANKNIFTY)
  │   └── Expected profit >= 2x total brokerage cost
  ├── Compute Greeks (BS model or market-implied)
  └── portfolio.add_signal() → Risk checks, lot sizing, position creation
```

### Step C: check_exits()

See [Section 14: Exit Engine](#14-exit-engine-16-layers) for the full 16-layer exit cascade.

---

## 4. DATA COLLECTION LAYER

### Spot Price (3-source priority)

```
Priority 1: WebSocket feed (Angel real-time, ~100ms latency)
            Tokens: NIFTY=99926000, BANKNIFTY=99926009, SENSEX=99919000
Priority 2: Angel REST API (LTP endpoint, ~500ms latency)
Priority 3: Historical daily close (last resort)
```

### Option Premium LTP (4-source fallback, 15s cache)

```
Priority 1: Zerodha Kite Connect (REST API)
Priority 2: TrueData WebSocket (real-time)
Priority 3: NSE/BSE MarketDataPipeline (direct option chains, 3-min update)
Priority 4: Angel SmartAPI BFO segment
Fallback:   Delta+Gamma+Theta approximation from entry (for premium tracking only)
```

### Market Data Pipeline (3-min refresh)

```
From NSE/BSE directly:
  ├── Option chains (all strikes, CE+PE, premium+OI+IV)
  ├── PCR (Put/Call OI ratio) per symbol
  ├── PCR Shift (change over session, e.g., 0.80 → 1.29 = bullish)
  ├── OI Sentiment (accumulation vs unwinding)
  ├── Premium Sentiment (CE vs PE premium % change)
  ├── India VIX (real-time)
  └── VIX Regime (low/normal/high + contrarian signal)
```

### Historical OHLC

```
Source: Angel SmartAPI daily bars
Auto-download: If existing data is >1 day stale
Used for: ATR(14), RSI(14), CPR calculation, efficiency ratio
Ghost Zone: 5-min bars aggregated to 4-hour blocks (5-day lookback)
```

---

## 5. INDICATOR COMPUTATION

### compute_indicators(symbol, ohlc)

```
ATR(14):
  True Range = max(H-L, |H-prev_close|, |L-prev_close|)
  ATR = 14-period EMA of True Range
  Used for: Volatility scaling, SL adjustment, regime detection

RSI(14):
  Standard Wilder RSI (0-100)
  Used for: Signal scoring, overbought/oversold filter

CPR (Central Pivot Range):
  Pivot = (H + L + C) / 3     ← previous day's data
  TC = (Pivot - L) + Pivot
  BC = Pivot - (H - Pivot)
  CPR Width = (TC - BC) / Pivot × 100
  Used for: CPR strategy entries, breakout/breakdown detection

Camarilla Levels:
  R3 = C + (H-L) × 1.1/4     R4 = C + (H-L) × 1.1/2
  S3 = C - (H-L) × 1.1/4     S4 = C - (H-L) × 1.1/2

VWAP:
  Cumulative(Price × Volume) / Cumulative(Volume)
  Intraday only, resets each day
  Used for: Trend confirmation (spot vs VWAP)

IV (Implied Volatility):
  Historical volatility × 1.15 (annualized %)
  Or: Newton-Raphson back-solve from market option LTP (50 iterations)
  Used for: OI/IV exit thresholds, signal scoring

Efficiency Ratio:
  |Close - Close[n]| / Sum(|Close[i] - Close[i-1]|) for i=1..n
  Scale: 0 to 1, >0.5 = trending
  Used for: Regime detection, choppy day filter, signal scoring
```

### Greeks Calculation (Black-Scholes)

```
d1 = [ln(S/K) + (r + σ²/2)T] / (σ√T)
d2 = d1 - σ√T

CE Price = S × N(d1) - K × e^(-rT) × N(d2)
PE Price = K × e^(-rT) × N(-d2) - S × N(-d1)

Delta_CE = N(d1)           Delta_PE = N(d1) - 1
Gamma    = φ(d1) / (S × σ√T)
Theta_CE = -[S × φ(d1) × σ / (2√T)] - r × K × e^(-rT) × N(d2)
Theta_PE = -[S × φ(d1) × σ / (2√T)] + r × K × e^(-rT) × N(-d2)
Vega     = S × φ(d1) × √T

Where N() = cumulative normal, φ() = normal PDF
Risk-free rate = 6.5% (Indian T-bill)
```

---

## 6. STRATEGY 1: CPR (CENTRAL PIVOT RANGE) — 30%

### Philosophy
Gomathi Shankar methodology. Breakout above TC = bullish, breakdown below BC = bearish. CPR width determines trade type (narrow = breakout, wide = mean reversion).

### Entry Logic

```
IF spot > TC:
  → BUY_CE (bullish breakout)
  → Only if CPR width >= 0.03% (skip unreliable narrow CPR)
  → VWAP check: Skip if intraday bearish AND spot below VWAP

IF spot < BC:
  → BUY_PE (bearish breakdown)
  → Same CPR width and VWAP filters

IF CPR width > 0.6% (wide range):
  → SELL_CE at extreme highs (mean reversion)
  → SELL_PE at extreme lows (mean reversion)
```

### Target/SL (Premium-based, v7.5)

```
Narrow CPR (<0.3%):   Target = 1.5× premium (+50%), SL = 0.5× (-50%)
Moderate (0.3-0.6%):  Target = 1.4× premium (+40%), SL = 0.5× (-50%)
Wide CPR (>0.6%):     Target = 1.3× premium (+30%), SL = 0.5× (-50%)

Volatility-adjusted SL:
  SL_multiplier = ATR_REFERENCE[NIFTY] / ATR_REFERENCE[symbol]
  BANKNIFTY SL tighter (2.7× more volatile than NIFTY)
  SENSEX SL even tighter (3.5× more volatile)
```

### Quality Score Factors
- CPR width (wider = higher quality for breakout)
- VWAP confirmation (spot on right side of VWAP)
- Efficiency ratio (trending market = higher quality)
- RSI not extreme (not overbought for CE, not oversold for PE)

---

## 7. STRATEGY 2: GAMMA BLAST — 40%

### Philosophy
Detects tight-range "coiled spring" days ready to explode. Enters on confirmed breakout direction. Highest risk-adjusted returns (Sharpe 4.42).

### Entry Logic

```
1. Calculate intraday body = spot - open
2. Calculate day_range = high - low

Entry conditions:
  ├── |body| > ATR × 0.15 (meaningful directional move)
  ├── day_range >= 0.3 × ATR (not dead flat)
  ├── prev_range <= 2.0 (not already extended from coil)
  ├── VWAP confirmation:
  │   ├── CE: spot > VWAP
  │   └── PE: spot < VWAP
  └── Direction:
      ├── body > 0 → BUY_CE (bullish expansion)
      └── body < 0 → BUY_PE (bearish expansion)
```

### Target/SL (Premium-based, v7.5)

```
Expiry day (DTE < 1):    Target = 1.6× premium (+60%), SL = 0.4× (-60%)
Non-expiry (DTE >= 1):   Target = 1.3-1.5× premium (+30-50%), SL = 0.5× (-50%)
```

### Key Strength
- 40-80% premium gains possible on gamma spike days
- Best risk-adjusted strategy (Sharpe 4.42)
- Works best on expiry days when gamma is highest

---

## 8. STRATEGY 3: GHOST ZONE v7 — 20%

### Philosophy
Manish Maheshwari institutional zone methodology. Detects institutional candles (pin bars, expansions, dojis) on 3×+ volume spikes. Creates supply/demand zones for retest entries.

### Zone Detection

```
1. Fetch 5-day, 5-minute candles from Angel SmartAPI
2. Aggregate to 4-hour blocks
3. For each 4H candle, check:
   ├── Volume spike: volume >= 3× average volume
   └── Candle type:
       ├── Pin bar: long wick, small body (institutional rejection)
       ├── Expansion: large body relative to range (institutional move)
       └── Doji: tiny body, large range (indecision at institution level)
4. Create zone at candle midpoint:
   ├── Demand zone: pin bar with lower wick (buying pressure)
   └── Supply zone: pin bar with upper wick (selling pressure)
```

### Entry Logic

```
IF spot retests 50% of a demand zone:
  → BUY_CE
  → SL below zone low

IF spot retests 50% of a supply zone:
  → BUY_PE
  → SL above zone high

FILTERS:
  ├── Only after trend exhaustion (NIFTY 300pt, BANKNIFTY/SENSEX 1000pt move)
  ├── OI validation: institutional concentration around zone levels
  └── 30-min cooldown after GTZ loss (prevents revenge trading)
```

---

## 9. STRATEGY 4: PCR+VWAP — 10%

### Philosophy
Institutional flow tracking via Put-Call Ratio shifts + VWAP trend confirmation.

### Entry Logic (CE Only — PE Disabled)

```
Bullish Entry (BUY_CE):
  ├── PCR shift >= +0.03 (SENSEX needs +0.05 due to noise)
  │   └── Shift = current PCR - session start PCR
  ├── Spot > VWAP (trend confirmation)
  ├── PCR < 1.5 (not extreme — avoid reversal)
  ├── Intraday body positive (spot > open)
  └── PCR source = PIPELINE or ANGEL_OI (not proxy)

PE trades DISABLED:
  Bearish PCR shift only 28-32% accurate in backtests
```

### Quality Score Calibration

```
PCR shift magnitude → Quality score:
  0.03 shift = score 15
  0.10 shift = score 50
  0.20 shift = score 100
  Linear interpolation between points
```

### Target/SL

```
Target = 1.5× premium (+50%)
SL = 0.5× premium (-50%)
```

---

## 10. STRATEGY 5: TREND RIDER — BONUS

### Philosophy
Rides big trend days after 1-hour confirmation window. Enters only between 10:15-10:30 AM.

### Entry Logic

```
Time window: 10:15 AM - 10:30 AM only

Entry conditions:
  ├── Session move > 0.5% in one direction (strong trend)
  ├── Gap < 1.5% (skip large gaps — reversal risk)
  ├── Signal score >= 5/10
  └── Max 1 Trend Rider entry per symbol per day

Direction:
  ├── Bullish trend → BUY_CE
  └── Bearish trend → BUY_PE

Target = 1.5× premium, SL = 0.65× premium (35% loss allowed — wide for trends)
TSL: Wide trail — 28% distance after 35% gain
```

---

## 11. SIGNAL SCORING & QUALITY FILTER

### compute_signal_score(signal, spot, indicators, vix)

```
Score range: 0-100

Components (weighted):
  1. Strategy conviction (0-30):
     ├── CPR: width, breakout strength
     ├── Gamma: body/ATR ratio, coil quality
     ├── Ghost Zone: zone strength, retest precision
     └── PCR+VWAP: PCR shift magnitude

  2. Technical confirmation (0-30):
     ├── VWAP alignment (+10 if spot on right side)
     ├── Efficiency ratio (+10 if >0.5)
     ├── RSI position (+10 if not extreme)
     └── ATR context (volatility appropriate)

  3. Market regime adjustment (0-20):
     ├── Trending market: +20 (strategies work best)
     ├── Sideways: +0 (neutral)
     └── Flat: -10 (strategies struggle)

  4. VIX regime adjustment (multiplier):
     ├── VIX < 14 (low vol): thresholds × 1.5 (harder to qualify)
     ├── VIX 14-20 (normal): thresholds × 1.0
     └── VIX > 20 (high vol): thresholds × 0.8 (easier to qualify)

MINIMUM SCORE: 50 (MIN_SIGNAL_SCORE)
  Below 50 → signal rejected entirely
```

---

## 12. PRE-EXECUTION FILTERS

### Filter Cascade (applied in order)

```
Filter 1:  MARKET HOURS — Must be 09:15-15:30, Mon-Fri
Filter 2:  EARLY MARKET BLOCK — No trades before 09:30 (15 min stabilization)
Filter 3:  LATE ENTRY BLOCK — No trades after 14:30 (need 50+ min to develop)
Filter 4:  DAILY TRADE CAP — Max 15 trades per day
Filter 5:  CONFLICT FILTER — If CE+PE signals for same symbol, keep higher score
Filter 6:  VIX HARD GATE — Block if VIX > 35 or VIX < 11
Filter 7:  DUPLICATE CHECK — Same strategy+symbol+direction already held?
Filter 8:  RE-ENTRY ESCALATION — 2nd entry needs score 65, 3rd 80, 4th 95
Filter 9:  DIRECTION CAP — Max 5 same-direction trades per symbol per day
Filter 10: CROSS-STRATEGY DEDUP — Another strategy holds same symbol+direction?
Filter 11: CONFLICTING POSITION — No BUY_CE + SELL_CE on same strike
Filter 12: DIRECTION FLIP — Close losing opposite if new score >= 70
Filter 13: RE-ENTRY COOLDOWN — 10 min after exit, 15 min after flip
Filter 14: GHOST ZONE COOLDOWN — 30 min after GTZ loss, same direction
Filter 15: CHOPPY DAY FILTER — Block after 11:00 if efficiency < 15%
Filter 16: SCORE THRESHOLD — Signal score >= escalated minimum?
Filter 17: PREMIUM QUALITY — BUY >= Rs 15 (NIFTY) / Rs 40 (BANKNIFTY)
Filter 18: PROFIT/COST RATIO — Expected profit >= 2× total brokerage
```

---

## 13. POSITION SIZING & CAPITAL MANAGEMENT

### Tiered Lot Sizing (v2.5.3)

```
Based on signal quality score + available capital:

Elite   (score >= 80): Allocate 30% of available capital
Strong  (score >= 60): Allocate 20% of available capital
Standard(score >= 50): Allocate 10% of available capital

Available capital = Segment capital - Total current exposure

BUY cost per lot  = premium × lot_size
SELL cost per lot = SPAN margin per lot

Hard caps per trade:
  NIFTY:     max 3 lots (3 × 65 = 195 qty)
  BANKNIFTY: max 3 lots (3 × 30 = 90 qty)
  SENSEX:    max 4 lots (4 × 20 = 80 qty)
```

### Drawdown Protection

```
If drawdown > 5% from high-water mark:
  → Force single lot regardless of score
  → Penalty function reduces sizing

equity_hwm = max(initial_capital, current_capital)
current_dd% = (hwm - capital) / hwm × 100
```

### Capital Structure

```
Total: Rs 8,00,000

Equity segment:   Rs 3,00,000 (NIFTY, BANKNIFTY, SENSEX)
Commodity segment: Rs 3,00,000 (GOLDM, SILVERM, CRUDEOILM)
Stock F&O segment: Rs 2,00,000 (individual stocks)

Max per trade: 25% of segment capital = Rs 75,000 (equity/commodity)
Shared pool: All strategies within a segment compete for same capital
```

---

## 14. EXIT ENGINE (16 LAYERS)

### Exit Priority Order (checked every scan cycle, every ~15 seconds)

```
LAYER 1: CIRCUIT BREAKER
  Condition: Daily realized loss > Rs 30,000 (10% of equity capital)
  Action:    Close ALL open equity positions immediately
  Telegram:  Send CIRCUIT BREAKER alert
  Result:    Trading halted for rest of day

LAYER 2: GRACE PERIOD
  Condition: Position held < 180 seconds (3 minutes)
  Action:    Skip ALL exit checks for this position
  Purpose:   Prevents noise-driven immediate exits

LAYER 3: EOD FORCE CLOSE
  Condition: Current time > 15:20 (10 min before market close)
  Action:    Force close position at current market premium
  Purpose:   Zero overnight risk

LAYER 4: GREEKS REFRESH (every 10 seconds — v15, was 5 min)
  Condition: 300+ seconds since last refresh
  Action:    Recalculate delta/gamma/theta from live option LTP
  Method:    Newton-Raphson IV back-solve → BS Greeks
  Purpose:   Accurate theta decay tracking, gamma shield

LAYER 5: THETA DECAY EXIT (expiry day only)
  Condition: DTE < 1 AND BUY position AND theta_burden > 999%
  Status:    EFFECTIVELY DISABLED (threshold set to 999%)
  History:   Was 10% — killed 82% of winning trades in 3 minutes
  Fallback:  If theta_burden > 5%, tighten SL to 90% of current premium

LAYER 6: TRADE INTELLIGENCE (v14.0)
  Condition: TradeIntelligence engine signals EXIT or TIGHTEN_SL
  Method:    Tracks premium readings, computes P(win), expected value
  Actions:
    ├── EXIT: P(win) too low, EV negative → close position
    └── TIGHTEN_SL: Raise trailing SL based on momentum analysis

LAYER 7: HOLD SCORE (signal-based, after 30 min)
  Computation:
    Re-run strategy indicators, compute fresh signal alignment
    Score 0-100: how well does current market support the trade?
  Actions:
    ├── STRONG (>= 60): Override TIME_EXIT, raise TSL aggressively
    ├── MODERATE (40-59): Normal exit rules apply
    └── WEAK (< 40) + losing + held > 1 hour → exit

LAYER 8: PCR SHIFT MONITOR (after 15 min hold)
  Condition: PCR shift direction opposes trade direction
  Example:   Holding BUY_CE but PCR turns bearish
  Action:    Tighten trailing SL by 20%

LAYER 9: OI/IV DYNAMIC EXITS
  Time-adaptive OI thresholds:
    09:15-10:15 AM: 50% OI change (first hour noise)
    10:15-02:00 PM: 35% OI change (normal session)
    02:00-03:30 PM: 40% OI change (close compression)
  IV thresholds:
    NIFTY: 30% IV change
    BANKNIFTY: 55% IV change (inherently volatile)
  Direction-aware IV logic:
    IV drop hurts BUY positions → tighten SL
    IV rise hurts SELL positions → tighten SL
  Strategy multipliers:
    Ghost Zone: 0.7× threshold (exits faster)
    Trend Rider: 2.0× threshold (tolerates more)
  Action: TIGHTEN SL (not force exit — v8.0 change)
  Min PnL to trigger: Rs 80 (covers 2× brokerage)

LAYER 10: OI+IV COMBO TIGHTENING
  Condition: OI change > 20% AND IV change > 25% simultaneously
  Action:    Tighten SL by additional 10%

LAYER 11: GAMMA SHIELD
  Condition: SELL position AND gamma > 0.002
  Action:    Force exit (massive gamma risk at expiry)

LAYER 12: MOMENTUM REVERSAL EXIT (v13.3)
  Condition: Spot moved 0.4% AGAINST trade direction after 20 min hold
  Example:   Holding BUY_CE, spot dropped 0.4% from entry
  Action:    Exit (directional bet invalidated)
  Validated: Saves Rs +6,974/week, hurts ZERO winning trades

LAYER 13: BREAKOUT FAILURE DETECTION (v13.0)
  Timer:     20 minutes after entry (regime-dependent)
  Skip:      On TRENDING regime days (let winners run)
  Condition: Peak gain < 1% at timer expiry
  Action:    Exit (breakout never materialized)
  Reversal:  DISABLED (lost Rs -8,632 across 8 trades, 0% WR)

LAYER 14: TIME-BASED EXIT
  Condition: Held > 4 hours
  Sub-check: If profit/loss < 15% of max_risk → exit (stale position)
  Override:  Skip if hold_score >= 60 (STRONG signals keep running)

LAYER 15: TRAILING STOP LOSS (TSL)
  See Section 15 for full TSL algorithm

LAYER 16: STATIC TARGET/SL HIT
  Condition: Premium hits hard target or hard SL
  Action:    Close position (final fallback)
  Note:      TSL usually triggers before static target due to TRAIL_TARGET
```

---

## 15. TRAILING STOP LOSS (TSL) — CORE EXIT MECHANISM

### Premium-Gain Based TSL (v13.0)

```
Track: peak_premium (highest premium seen since entry)
       gain_pct = (peak_premium - entry_premium) / entry_premium × 100

PHASE 0: MICRO-TRAIL (5% gain)
  Trigger:   gain_pct >= 5% AND held >= 180 seconds
  SL:        peak_premium × 0.55 (keep 55% of peak gain)
  Purpose:   Protect tiny gains, prevent round-trips

PHASE 1: BREAKEVEN LOCK (10% gain)
  Trigger:   gain_pct >= 10%
  SL:        entry_premium (breakeven — guaranteed no loss)
  Flag:      breakeven_locked = True
  Purpose:   Remove downside risk once trade is working

PHASE 2: STANDARD TRAIL (20% gain)
  Trigger:   gain_pct >= 20%
  SL:        peak_premium × 0.80 (trail 20% below peak)
  Purpose:   Let winners run while protecting profits

PHASE 3: TIGHT TRAIL (35% gain)
  Trigger:   gain_pct >= 35%
  SL:        peak_premium × 0.88 (trail 12% below peak)
  Purpose:   Lock in majority of profit on big winners

TRAILING TARGET (v10.1):
  When static target is hit:
    → Extend target by 20% of current premium
    → Tighten TSL to 15% below peak
    → Max 5 extensions (safety cap)
  Purpose: Ride momentum beyond initial target

For SELL positions: Logic inverted
  Profit = premium going DOWN
  SL = if premium rises ABOVE trailing SL → exit
```

### TSL Decision Flow

```
Every exit check cycle (~15 seconds):
  1. Get current market premium (4-source fallback)
  2. Update peak_premium if current > peak
  3. Calculate gain_pct from entry
  4. Determine active TSL phase (highest qualified phase)
  5. Calculate phase-specific trailing_sl
  6. If current_premium < trailing_sl:
     → EXIT with reason "TSL_PHASE_X"
  7. If hold_score is STRONG:
     → Raise TSL by additional 5% (protect more aggressively)
```

---

## 16. MARKET REGIME DETECTION

### RegimeDetector (market_regime.py)

```
Input: Rolling 60-minute price history (240 readings at 15s interval)

TRENDING regime:
  ├── Session move > 0.4% (price moved meaningfully)
  └── Efficiency ratio > 0.4 (move is directional, not choppy)

SIDEWAYS regime:
  ├── Normal range-bound trading
  └── Default when not TRENDING or FLAT

FLAT regime:
  ├── Session move < 0.15% (almost no movement)
  └── Efficiency ratio < 0.25 (very choppy)

Hysteresis: 10-cycle minimum before switching regimes (~2.5 min)
  Purpose: Prevent rapid oscillation between regimes
```

### Regime-Adaptive Parameters

```
                    TRENDING    SIDEWAYS    FLAT
TSL trail distance: Wider       Normal      Tighter
Breakout fail:      DISABLED    Enabled     Enabled
Re-entry cooldown:  Shorter     Normal      Longer
Score threshold:    Lower       Normal      Higher
```

---

## 17. RISK MANAGEMENT

### Per-Trade Risk

```
Max capital per trade: Rs 75,000 (25% of segment)
Max positions per symbol: 3 (prevent cascade)
Max daily trades: 15 (hard cap)
Max same-direction per symbol: 5 per day
```

### Daily Risk

```
Circuit breaker: Rs 30,000 daily loss (10% of capital)
  → Closes ALL positions, halts trading for day
  → Sends Telegram alert
```

### Position Risk

```
Drawdown > 5%: Force single lot (penalty function)
Re-entry escalation: +15 score per re-entry attempt
Direction flip: Requires score >= 70
VIX gate: Block all entries if VIX > 35 or VIX < 11
```

### Cooldowns

```
Grace period after entry:          180 seconds (3 min)
Re-entry after any exit:           600 seconds (10 min)
Re-entry after DIRECTION_FLIP:     900 seconds (15 min)
Ghost Zone after GTZ loss:         1800 seconds (30 min)
```

---

## 18. DATA SOURCES & FALLBACK CHAIN

### Option LTP (4-source chain, 15s cache)

```
┌─────────────────────────┐
│  Source 1: Zerodha Kite │ ← Primary (best accuracy)
│  REST API for LTP       │
└──────────┬──────────────┘
           │ fails
┌──────────▼──────────────┐
│  Source 2: TrueData WS  │ ← Real-time WebSocket
│  live_port=8086         │
└──────────┬──────────────┘
           │ fails
┌──────────▼──────────────┐
│  Source 3: NSE/BSE      │ ← MarketDataPipeline
│  Direct option chains   │    3-min refresh
└──────────┬──────────────┘
           │ fails
┌──────────▼──────────────┐
│  Source 4: Angel BFO    │ ← SmartAPI BFO segment
│  REST API for LTP       │
└──────────┬──────────────┘
           │ ALL fail
┌──────────▼──────────────┐
│  Fallback: Approximation│ ← Delta×ΔSpot + 0.5×Gamma×ΔSpot²
│  From entry Greeks       │    + Theta×hours/24
│  (tracking only,         │    NOT used for entry/exit decisions
│   warns after 30 min)    │
└─────────────────────────┘
```

---

## 19. COMMODITY BOT DIFFERENCES

### commodity_paper_trader.py (4272 lines)

```
Symbols: GOLDM, SILVERM, CRUDEOILM (mini contracts)
Capital: Rs 3,00,000 (separate pool)
Trading hours: 09:00 AM - 11:30 PM IST (MCX extended hours)

Lot sizes: 1 lot each
  Multipliers: Gold=10, Silver=5, CrudeOil=10
  Margins: Rs 15,000 (gold/silver), Rs 8,000 (crude)

Strategies: 3 active
  CPR:         45% allocation
  Gamma Blast: 35% allocation
  Ghost Zone:  20% allocation
  PCR+VWAP:    Not available (no MCX PCR data)

Greeks model: Black-76 (options on futures)
  Instead of Black-Scholes (options on spot)
  Difference: Forward price instead of spot in BS formula

TSL: Adjusted for commodity volatility (wider ranges)
Theta exit: DISABLED (same fix as equity)

STATUS: BLOCKED — Angel MCX API returns "Access denied"
  Paper trading only with local CSV data
```

---

## 20. STOCK F&O BOT DIFFERENCES

### stock_paper_trader.py (4402 lines)

```
Symbols: Individual F&O stocks (SBIN, RELIANCE, HDFCBANK, etc.)
Capital: Rs 2,00,000 (separate pool)
Expiry: Monthly only (last Thursday)

Lot sizes: Stock-specific (from stock_fno_config.py)
Max positions: 2 per stock, 5 total

Strategies: 3 active
  CPR Breakout:  Morning scanner for pre-market setup
  Gamma Blast:   Same logic, stock-specific ATR
  PCR+VWAP:      Stock-specific OI data

Unique features:
  ├── Morning CPR scanner (finds pre-market setups)
  ├── Multi-symbol watchlist support
  ├── Individual stock ATR_REFERENCE per stock
  └── Stock-specific lot sizes and margins
```

---

## 21. DEPLOYMENT & INFRASTRUCTURE

### VPS

```
Provider: Vultr Ubuntu
IP: 65.20.69.104
SSH: ssh -i "$USERPROFILE/.ssh/id_rsa_vultr" root@65.20.69.104
Code: /root/algo_trading/
Logs: /root/algo_trading/logs/
```

### Systemd Services

```
algo-trading.service:
  ├── Runs paper_trader.py (equity: NIFTY/BANKNIFTY/SENSEX)
  ├── Runs commodity_paper_trader.py (MCX: GOLDM/SILVERM/CRUDEOILM)
  └── Env: ANGEL_CRED_FILE=/root/angel_creds.txt

algo-stock-trading.service:
  ├── Runs stock_paper_trader.py (individual F&O stocks)
  └── Env: ANGEL_CRED_FILE=/root/angel_creds.txt

Commands:
  systemctl restart algo-trading algo-stock-trading
  systemctl status algo-trading
  journalctl -u algo-trading -f
```

### Deployment Workflow

```
1. Edit code locally: D:/AlgoTrading/algo_trading/
2. SCP to VPS: scp -i key file.py root@65.20.69.104:/root/algo_trading/
3. Restart service: systemctl restart algo-trading
4. Monitor logs: tail -f /root/algo_trading/logs/unified_paper_YYYYMMDD.log
```

### Signal Logging

```
Every scan cycle, ALL signals (executed + skipped) are logged:
  File: paper_trades/signals_YYYYMMDD.csv
  Columns: timestamp, strategy, symbol, type, strike, premium, spot, dte,
           delta, gamma, theta, vega, iv, volume, oi, pcr, vwap,
           target, sl, quality_score, reason, executed

Portfolio state persisted: paper_trades/portfolio_state.json
  Contains: capital, positions[], closed_trades[], daily_pnl{}
  Auto-loaded on restart (restart-safe design)

Daily report: paper_trades/daily_report_YYYYMMDD.json
  Contains: summary, positions, trades_today
```

---

## 22. KNOWN BUGS & FIXES

### Fixed

```
1. THETA_DECAY_EXIT (v11.1):
   Bug: 10% threshold fires on ALL near-expiry options
   Impact: Killed 82% of winning trades within 3 minutes
   Fix: Set threshold to 999% (effectively disabled)
   Validation: Backtest PnL improved from +0.4% to +6.3%

2. BREAKOUT_FAIL_CHECK (v13.0):
   Bug: 5-minute check exits trades before they develop
   Impact: Premature exits on trades that would have won
   Fix: Extended to 20 minutes, regime-dependent
   Additional: REVERSE disabled (0% WR, -Rs 8,632 loss)

3. ESCALATED_COOLDOWN (v10):
   Bug: 20-40 min block after theta-caused losses
   Impact: Missed legitimate re-entry opportunities
   Fix: Removed entirely (was part of THETA bug cascade)

4. OI/IV EXIT THRESHOLDS (v8.0):
   Bug: 15% OI threshold caused 82% premature exits
   Impact: Closed winning trades on normal OI fluctuations
   Fix: Raised to 35-50% (time-adaptive), tighten SL instead of force exit
```

### Outstanding

```
5. COMMODITY BOT BLOCKED:
   Issue: Angel MCX API rate limits → "Access denied"
   Impact: Cannot live trade MCX commodities
   Status: Paper trading only with local CSV data

6. CONSISTENCY PROBLEM (identified in backtesting):
   Issue: 95-96% of profits come from top 3-4 trades (crash days)
   Impact: Without rare big wins, strategies are NET LOSS
   Status: Researching consistent small-win strategies
```

---

## 23. COST MODEL

### Per-Trade Costs

```
Brokerage:          Rs 20 per trade (flat — Angel One)
STT (on sell):      0.0625% of premium × lot_size
Exchange charges:   0.05% per side
GST:                18% on brokerage (Rs 3.60 per trade)
Stamp duty:         0.003% (buy side only)
Slippage:           ~0.5% per side (estimated)

Total roundtrip cost: ~1.2% of trade value
  This means: A trade must gain > 1.2% just to break even
  Premium filter: Expected profit >= 2× total cost (MIN_PROFIT_TO_COST_RATIO = 2.0)
```

### Lot Sizes & Margins

```
Symbol      Lot Size    Margin/Lot    Strike Interval
NIFTY       65          Rs 1,00,000   50
BANKNIFTY   30          Rs 95,000     100
SENSEX      20          Rs 70,000     100
GOLDM       10*         Rs 15,000     varies
SILVERM     5*          Rs 15,000     varies
CRUDEOILM   10*         Rs 8,000      varies
(* = multiplier, not traditional lots)
```

---

## 24. KEY CONSTANTS REFERENCE

### Timing

```
MARKET_OPEN              = 09:15
MARKET_CLOSE             = 15:30
PRE_MARKET               = 09:00
EQUITY_FIRST_TRADE_TIME  = 09:30 (no trades in first 15 min)
LAST_ENTRY_TIME          = 14:30 (no new entries after this)
EOD_FORCE_CLOSE          = 15:20 (10 min before close)
SCAN_INTERVAL            = 1 second (v15, was 5 minutes)
```

### Risk

```
INITIAL_CAPITAL          = Rs 3,00,000 (equity)
MAX_RISK_PCT             = 25% per trade
MAX_DAILY_LOSS           = Rs 30,000 (10% circuit breaker)
MAX_POSITIONS_PER_SYMBOL = 3
MAX_TRADES_PER_DAY       = 15
MAX_SAME_DIRECTION       = 5 per symbol per day
```

### Signal Quality

```
MIN_SIGNAL_SCORE         = 50
MIN_PREMIUM_BUY          = Rs 15 (NIFTY)
MIN_PREMIUM_BUY_BN       = Rs 40 (BANKNIFTY)
MIN_PREMIUM_SELL         = Rs 20
MIN_PROFIT_TO_COST_RATIO = 2.0
SCORE_ESCALATION         = +15 per re-entry
DIRECTION_FLIP_MIN_SCORE = 70
```

### TSL

```
MICRO_GAIN               = 5%,  trail 45%
BREAKEVEN_GAIN           = 10%
TRAIL_GAIN               = 20%, trail 20%
TIGHT_GAIN               = 35%, trail 12%
TARGET_EXTEND            = 20% of premium, max 5 extensions
```

### Cooldowns

```
GRACE_PERIOD             = 180s (3 min)
REENTRY_COOLDOWN         = 600s (10 min)
DIRECTION_FLIP_COOLDOWN  = 900s (15 min)
GHOST_ZONE_COOLDOWN      = 1800s (30 min)
GREEKS_REFRESH           = 10s (v15, was 300s/5 min)
```

### VIX

```
VIX_LOW_THRESHOLD        = 14 (multiply filters × 1.5)
VIX_HIGH_THRESHOLD       = 20 (multiply filters × 0.8)
VIX_BLOCK_HIGH           = 35 (block ALL entries)
VIX_BLOCK_LOW            = 11 (block ALL entries)
```

---

## END-TO-END FLOW SUMMARY

```
09:00  System boots → connects Angel API → loads instruments → loads portfolio
09:15  Market opens → scan starts (every 1 second — v15 tick-by-tick)
09:30  Trading enabled → signals can execute

EACH SCAN CYCLE (every 1 second — v15):
  │
  ├─ COLLECT: Spot prices, OHLC, VIX, PCR, OI
  ├─ COMPUTE: ATR, RSI, CPR, VWAP, IV, Efficiency, Regime
  ├─ GENERATE: Run 4 strategies → produce signal list
  ├─ FILTER: 18 pre-execution filters (quality, risk, cooldowns)
  ├─ SIZE: Tiered lot sizing based on score + available capital
  ├─ EXECUTE: Open paper position, record Greeks, set TSL
  ├─ MONITOR: 16-layer exit engine on all open positions
  │   ├─ Update premium (4-source chain)
  │   ├─ Refresh Greeks (every 10s — v15, pure BS math)
  │   ├─ Check TSL phases (micro → breakeven → trail → tight)
  │   ├─ Check OI/IV/PCR shifts → tighten SL
  │   ├─ Check momentum reversal, breakout failure
  │   └─ Check hold score, time limits
  ├─ LOG: All signals to CSV (executed + skipped)
  └─ REPORT: Print status (positions, PnL, capital)

14:30  No new entries allowed
15:20  Force close all remaining positions
15:30  Market close → save daily report → shutdown
```

---

*Document generated: 2026-03-20*
*System version: paper_trader.py v7.6 / v14.0*
*Total codebase: ~14,500 lines across 3 bots + 8 support modules*
