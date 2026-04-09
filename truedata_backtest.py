"""
TICK-BY-TICK BACKTEST using TrueData 1-min spot data
=====================================================
Simulates our EXACT CPR strategy with:
- Real 1-min spot prices from TrueData
- Black-Scholes option pricing at each tick
- All exit logic: TSL, Target, THETA_DECAY, BREAKOUT_FAIL, EOD
- Real transaction costs (brokerage + STT + exchange charges)

Covers: Mar 12-13, 2026 (all available data)
"""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
from math import log, sqrt, exp
from scipy.stats import norm
import json

# ====================================================================
# CONFIGURATION (exact copy from paper_trader.py)
# ====================================================================
INITIAL_CAPITAL = 300000
RISK_FREE_RATE = 0.065
LOT_SIZES = {'NIFTY': 65, 'BANKNIFTY': 30, 'SENSEX': 20}
STRIKE_INTERVALS = {'NIFTY': 50, 'BANKNIFTY': 100, 'SENSEX': 100}
BROKERAGE = 20
STT_SELL = 0.000625
EXCHANGE_CHARGES = 0.0005
GST_RATE = 0.18

# Exit parameters
GRACE_PERIOD_SECONDS = 180
TSL_MICRO_GAIN_PCT = 5
TSL_MICRO_TRAIL_DISTANCE_PCT = 60
TSL_MICRO_MIN_HOLD_SECONDS = 300
TSL_BREAKEVEN_GAIN_PCT = 15
TSL_TRAIL_GAIN_PCT = 25
TSL_TRAIL_DISTANCE_PCT = 30
TSL_TIGHT_GAIN_PCT = 40
TSL_TIGHT_DISTANCE_PCT = 20
BREAKOUT_FAIL_CHECK_MINUTES = 5
BREAKOUT_FAIL_MIN_GAIN_PCT = 2
BREAKOUT_FAIL_REVERSE_DROP_PCT = 15
THETA_BURDEN_EXIT_PCT = 10.0
THETA_BURDEN_TIGHTEN_PCT = 5.0
HOLD_SCORE_MIN_HOLD_MINS = 30
HOLD_SCORE_WEAK = 40
MAX_POSITIONS_PER_SYMBOL = 3
REENTRY_COOLDOWN_SECONDS = 600

# ====================================================================
# BLACK-SCHOLES
# ====================================================================
def bs_price(S, K, T, r, sigma, opt_type='CE'):
    if T <= 0: T = 1e-6
    if sigma <= 0: sigma = 0.01
    d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    if opt_type == 'CE':
        return S * norm.cdf(d1) - K * exp(-r*T) * norm.cdf(d2)
    else:
        return K * exp(-r*T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def bs_greeks(S, K, T, r, sigma, opt_type='CE'):
    if T <= 0: T = 1e-6
    if sigma <= 0: sigma = 0.01
    d1 = (log(S/K) + (r + 0.5*sigma**2)*T) / (sigma*sqrt(T))
    d2 = d1 - sigma*sqrt(T)
    n_d1 = norm.pdf(d1)

    if opt_type == 'CE':
        delta = norm.cdf(d1)
        price = S * norm.cdf(d1) - K * exp(-r*T) * norm.cdf(d2)
    else:
        delta = norm.cdf(d1) - 1
        price = K * exp(-r*T) * norm.cdf(-d2) - S * norm.cdf(-d1)

    gamma = n_d1 / (S * sigma * sqrt(T))
    theta = -(S * n_d1 * sigma) / (2 * sqrt(T)) - r * K * exp(-r*T) * (norm.cdf(d2) if opt_type == 'CE' else norm.cdf(-d2))
    theta = theta / 365  # per day
    vega = S * n_d1 * sqrt(T) / 100

    return {
        'price': max(price, 0.05), 'delta': delta, 'gamma': gamma,
        'theta': theta, 'vega': vega, 'iv': sigma
    }

def calc_costs(premium, qty, is_sell=False):
    turnover = premium * qty
    brok = min(BROKERAGE, turnover * 0.0003)
    stt = turnover * STT_SELL if is_sell else 0
    exch = turnover * EXCHANGE_CHARGES
    gst = (brok + exch) * GST_RATE
    return round(brok + stt + exch + gst, 2)

# ====================================================================
# CPR CALCULATION
# ====================================================================
def compute_cpr(prev_high, prev_low, prev_close):
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * pivot - bc
    cpr_width = abs(tc - bc) / prev_close * 100
    h_range = prev_high - prev_low
    cam_r3 = prev_close + h_range * 1.1 / 4
    cam_s3 = prev_close - h_range * 1.1 / 4
    cam_r4 = prev_close + h_range * 1.1 / 2
    cam_s4 = prev_close - h_range * 1.1 / 2
    return {
        'pivot': pivot, 'bc': bc, 'tc': tc, 'cpr_width': cpr_width,
        'cam_r3': cam_r3, 'cam_s3': cam_s3, 'cam_r4': cam_r4, 'cam_s4': cam_s4
    }

# ====================================================================
# ATR from daily bars
# ====================================================================
def compute_atr(daily_bars, period=14):
    if len(daily_bars) < period:
        return (daily_bars['h'] - daily_bars['l']).mean()
    return (daily_bars['h'].tail(period) - daily_bars['l'].tail(period)).mean()

def compute_hv(daily_bars, period=20):
    if len(daily_bars) < period + 1:
        return 0.20
    log_ret = np.log(daily_bars['c'] / daily_bars['c'].shift(1)).dropna()
    return log_ret.tail(period).std() * np.sqrt(252)

# ====================================================================
# TRADE POSITION
# ====================================================================
class Position:
    def __init__(self, symbol, signal_type, strike, entry_premium, entry_spot,
                 entry_time, lot_size, iv, dte, greeks, target, sl, reason):
        self.symbol = symbol
        self.signal_type = signal_type
        self.strike = strike
        self.entry_premium = entry_premium
        self.entry_spot = entry_spot
        self.entry_time = entry_time
        self.lot_size = lot_size
        self.iv = iv
        self.dte = dte
        self.greeks = greeks
        self.target = target
        self.sl = sl
        self.reason = reason
        self.opt_type = 'CE' if 'CE' in signal_type else 'PE'
        self.is_sell = 'SELL' in signal_type

        self.current_premium = entry_premium
        self.peak_premium = entry_premium
        self.trailing_sl = None
        self.peak_gain_pct = 0
        self.exit_premium = None
        self.exit_time = None
        self.exit_reason = None
        self.pnl = None

    def update(self, spot, current_time):
        """Update option premium using BS model at current spot/time"""
        elapsed_days = (current_time - self.entry_time).total_seconds() / 86400
        remaining_dte = max(self.dte - elapsed_days, 1e-6)
        T = remaining_dte / 365

        self.current_premium = bs_price(spot, self.strike, T, RISK_FREE_RATE, self.iv, self.opt_type)
        self.current_premium = max(self.current_premium, 0.05)

        # Update greeks
        self.greeks = bs_greeks(spot, self.strike, T, RISK_FREE_RATE, self.iv, self.opt_type)

        if self.current_premium > self.peak_premium:
            self.peak_premium = self.current_premium

        gain_pct = (self.current_premium - self.entry_premium) / self.entry_premium * 100
        if gain_pct > self.peak_gain_pct:
            self.peak_gain_pct = gain_pct

    @property
    def unrealized_pnl(self):
        diff = self.current_premium - self.entry_premium
        if self.is_sell:
            diff = -diff
        raw = diff * self.lot_size
        costs = calc_costs(self.entry_premium, self.lot_size) + calc_costs(self.current_premium, self.lot_size, True)
        return round(raw - costs, 2)

    def close(self, exit_premium, exit_time, reason):
        self.exit_premium = exit_premium
        self.exit_time = exit_time
        self.exit_reason = reason
        diff = exit_premium - self.entry_premium
        if self.is_sell:
            diff = -diff
        raw = diff * self.lot_size
        costs = calc_costs(self.entry_premium, self.lot_size) + calc_costs(exit_premium, self.lot_size, True)
        self.pnl = round(raw - costs, 2)

# ====================================================================
# BACKTEST ENGINE
# ====================================================================
class TickByTickBacktest:
    def __init__(self):
        self.positions = []
        self.closed_trades = []
        self.capital = INITIAL_CAPITAL
        self.daily_pnl = {}
        self.cooldowns = {}  # symbol -> last_exit_time
        self.trade_count = {}  # date -> count
        self.loss_count = {}  # (symbol, direction) -> consecutive losses

    def get_iv_for_symbol(self, symbol, hv, dte):
        """Estimate IV based on HV and DTE"""
        # Near expiry: IV typically 1.3-1.8x HV
        # Far from expiry: IV typically 1.1-1.3x HV
        if dte <= 1:
            iv_mult = 1.5
        elif dte <= 3:
            iv_mult = 1.3
        else:
            iv_mult = 1.15
        iv = hv * iv_mult
        return max(min(iv, 0.80), 0.10)

    def select_strike(self, symbol, spot, opt_type):
        """Select ATM strike"""
        interval = STRIKE_INTERVALS.get(symbol, 50)
        atm = round(spot / interval) * interval
        if opt_type == 'PE':
            return atm  # ATM for PE
        else:
            return atm  # ATM for CE

    def check_cpr_entry(self, symbol, spot, cpr, ohlc_today, hv, atr, dte, current_time, vwap):
        """Check if CPR signal triggers"""
        signals = []
        cpr_w = cpr['cpr_width']

        # Set targets based on CPR width
        if cpr_w < 0.3:
            target_mult, sl_mult = 1.5, 0.5
        elif cpr_w <= 0.6:
            target_mult, sl_mult = 1.4, 0.5
        else:
            target_mult, sl_mult = 1.3, 0.5

        iv = self.get_iv_for_symbol(symbol, hv, dte)
        T = dte / 365

        # BUY_CE: Spot above TC (bullish breakout)
        if spot > cpr['tc']:
            intraday_body = spot - ohlc_today['open']
            if not (intraday_body < 0 and vwap > 0 and spot < vwap):
                strike = self.select_strike(symbol, spot, 'CE')
                g = bs_greeks(spot, strike, T, RISK_FREE_RATE, iv, 'CE')
                premium = g['price']
                if premium > 15:
                    use_target = premium * target_mult if ohlc_today['high'] > cpr['cam_r3'] else premium * (target_mult - 0.2)
                    signals.append({
                        'type': 'BUY_CE_CPR', 'strike': strike, 'premium': premium,
                        'greeks': g, 'iv': iv, 'target': use_target, 'sl': premium * sl_mult,
                        'reason': f"CPR bullish breakout above TC={cpr['tc']:.0f}, CPR_w={cpr_w:.3f}%"
                    })

        # BUY_PE: Spot below BC (bearish breakdown)
        elif spot < cpr['bc']:
            intraday_body = spot - ohlc_today['open']
            if not (intraday_body > 0 and vwap > 0 and spot > vwap):
                strike = self.select_strike(symbol, spot, 'PE')
                g = bs_greeks(spot, strike, T, RISK_FREE_RATE, iv, 'PE')
                premium = g['price']
                if premium > 15:
                    use_target = premium * target_mult if ohlc_today['low'] < cpr['cam_s3'] else premium * (target_mult - 0.2)
                    signals.append({
                        'type': 'BUY_PE_CPR', 'strike': strike, 'premium': premium,
                        'greeks': g, 'iv': iv, 'target': use_target, 'sl': premium * sl_mult,
                        'reason': f"CPR bearish breakdown below BC={cpr['bc']:.0f}, CPR_w={cpr_w:.3f}%"
                    })

        return signals

    def check_exits(self, spot, current_time):
        """Check all exit conditions for open positions"""
        to_close = []

        for pos in self.positions:
            pos.update(spot, current_time)
            elapsed = (current_time - pos.entry_time).total_seconds()

            # Grace period
            if elapsed < GRACE_PERIOD_SECONDS:
                continue

            # EOD force close
            if current_time.time() > dtime(15, 20):
                to_close.append((pos, pos.current_premium, 'EOD_FORCE_CLOSE'))
                continue

            premium = pos.current_premium
            entry_prem = pos.entry_premium
            gain_pct = (premium - entry_prem) / entry_prem * 100
            peak_gain_pct = pos.peak_gain_pct

            # ---- THETA_DECAY_EXIT (the bug we're testing) ----
            elapsed_days = elapsed / 86400
            remaining_dte = max(pos.dte - elapsed_days, 0)
            if remaining_dte < 1 and not pos.is_sell:
                theta_val = abs(pos.greeks.get('theta', 0))
                if premium > 0 and theta_val > 0:
                    theta_burden_pct = theta_val / premium * 100
                    if theta_burden_pct > THETA_BURDEN_EXIT_PCT and pos.unrealized_pnl < 0:
                        to_close.append((pos, premium, f'THETA_DECAY_EXIT (burden={theta_burden_pct:.1f}%)'))
                        continue

            # ---- BREAKOUT_FAIL (5-min check) ----
            if GRACE_PERIOD_SECONDS < elapsed <= BREAKOUT_FAIL_CHECK_MINUTES * 60:
                if entry_prem > 0:
                    if gain_pct < BREAKOUT_FAIL_MIN_GAIN_PCT:
                        # Didn't gain enough — exit
                        if gain_pct < -BREAKOUT_FAIL_REVERSE_DROP_PCT:
                            to_close.append((pos, premium, f'BREAKOUT_FAIL_REVERSE (gain={gain_pct:.1f}%)'))
                        else:
                            to_close.append((pos, premium, f'BREAKOUT_FAIL (gain={gain_pct:.1f}%)'))
                        continue

            # ---- STATIC SL ----
            if not pos.is_sell and premium <= pos.sl:
                to_close.append((pos, premium, f'SL_HIT (prem={premium:.2f} <= SL={pos.sl:.2f})'))
                continue

            # ---- TARGET HIT (with trailing) ----
            if premium >= pos.target:
                # Don't exit immediately - set tight TSL
                pos.trailing_sl = premium * (1 - 0.15)  # 15% below peak

            # ---- TRAILING SL PHASES ----
            if peak_gain_pct >= TSL_TIGHT_GAIN_PCT:
                # Phase 3: Tight trail
                trail_sl = pos.peak_premium * (1 - TSL_TIGHT_DISTANCE_PCT/100)
                if pos.trailing_sl is None or trail_sl > pos.trailing_sl:
                    pos.trailing_sl = trail_sl
            elif peak_gain_pct >= TSL_TRAIL_GAIN_PCT:
                # Phase 2: Normal trail
                trail_sl = pos.peak_premium * (1 - TSL_TRAIL_DISTANCE_PCT/100)
                if pos.trailing_sl is None or trail_sl > pos.trailing_sl:
                    pos.trailing_sl = trail_sl
            elif peak_gain_pct >= TSL_BREAKEVEN_GAIN_PCT:
                # Phase 1: Lock breakeven
                be_sl = entry_prem * 1.005  # Tiny profit above entry
                if pos.trailing_sl is None or be_sl > pos.trailing_sl:
                    pos.trailing_sl = be_sl
            elif peak_gain_pct >= TSL_MICRO_GAIN_PCT and elapsed >= TSL_MICRO_MIN_HOLD_SECONDS:
                # Phase 0: Micro trail
                trail_sl = entry_prem + (pos.peak_premium - entry_prem) * (1 - TSL_MICRO_TRAIL_DISTANCE_PCT/100)
                if pos.trailing_sl is None or trail_sl > pos.trailing_sl:
                    pos.trailing_sl = trail_sl

            # Check trailing SL
            if pos.trailing_sl and premium <= pos.trailing_sl:
                to_close.append((pos, premium, f'TSL_HIT (phase peak={peak_gain_pct:.1f}%, prem={premium:.2f} <= TSL={pos.trailing_sl:.2f})'))
                continue

            # ---- TIME EXIT (>4 hours stagnant) ----
            hours_held = elapsed / 3600
            if hours_held > 4:
                max_risk = entry_prem * pos.lot_size
                profit_pct = (pos.unrealized_pnl / max(max_risk, 1)) * 100
                if abs(profit_pct) < 15:
                    to_close.append((pos, premium, f'TIME_EXIT ({hours_held:.1f}h, profit={profit_pct:.1f}%)'))
                    continue

        # Execute closes
        for pos, exit_prem, reason in to_close:
            pos.close(exit_prem, current_time, reason)
            self.closed_trades.append(pos)
            self.positions.remove(pos)
            self.cooldowns[pos.symbol] = current_time

            # Track daily PnL
            day_key = current_time.strftime('%Y-%m-%d')
            self.daily_pnl[day_key] = self.daily_pnl.get(day_key, 0) + pos.pnl
            self.capital += pos.pnl

    def can_enter(self, symbol, current_time, signal_type):
        """Check cooldowns and position limits"""
        # Position limit
        sym_positions = [p for p in self.positions if p.symbol == symbol]
        if len(sym_positions) >= MAX_POSITIONS_PER_SYMBOL:
            return False, 'MAX_POSITIONS'

        # Cooldown check
        last_exit = self.cooldowns.get(symbol)
        if last_exit:
            elapsed = (current_time - last_exit).total_seconds()
            if elapsed < REENTRY_COOLDOWN_SECONDS:
                return False, f'COOLDOWN ({elapsed:.0f}s < {REENTRY_COOLDOWN_SECONDS}s)'

        # Escalated cooldown (from live system)
        key = (symbol, 'CE' if 'CE' in signal_type else 'PE')
        loss_count = self.loss_count.get(key, 0)
        if loss_count > 0 and last_exit:
            escalated = REENTRY_COOLDOWN_SECONDS * (1 + loss_count)
            elapsed = (current_time - last_exit).total_seconds()
            if elapsed < escalated:
                return False, f'ESCALATED_COOLDOWN (losses={loss_count}, need={escalated:.0f}s, elapsed={elapsed:.0f}s)'

        # Daily trade limit
        day_key = current_time.strftime('%Y-%m-%d')
        if self.trade_count.get(day_key, 0) >= 15:
            return False, 'MAX_DAILY_TRADES'

        return True, 'OK'

    def enter_trade(self, symbol, signal, current_time, spot, dte):
        """Enter a new position"""
        lot_size = LOT_SIZES.get(symbol, 50)
        pos = Position(
            symbol=symbol,
            signal_type=signal['type'],
            strike=signal['strike'],
            entry_premium=signal['premium'],
            entry_spot=spot,
            entry_time=current_time,
            lot_size=lot_size,
            iv=signal['iv'],
            dte=dte,
            greeks=signal['greeks'],
            target=signal['target'],
            sl=signal['sl'],
            reason=signal['reason']
        )
        self.positions.append(pos)
        day_key = current_time.strftime('%Y-%m-%d')
        self.trade_count[day_key] = self.trade_count.get(day_key, 0) + 1
        return pos

    def run_backtest(self, spot_data_dict, daily_data_dict, run_label=""):
        """
        Run backtest on 1-min spot data.

        spot_data_dict: {symbol: DataFrame with time, o, h, l, c columns}
        daily_data_dict: {symbol: DataFrame with date, o, h, l, c columns}
        """
        print(f"\n{'='*80}")
        print(f"TICK-BY-TICK BACKTEST: {run_label}")
        print(f"{'='*80}")
        print(f"Initial Capital: Rs {self.capital:,.0f}")
        print()

        # Get all unique trading dates
        all_times = []
        for sym, df in spot_data_dict.items():
            all_times.extend(df['time'].tolist())
        all_times = sorted(set(all_times))

        # Group by date
        dates = sorted(set(t.date() for t in all_times))

        for trade_date in dates:
            print(f"\n--- {trade_date} ---")
            day_bars = [t for t in all_times if t.date() == trade_date]
            day_bars.sort()

            # Get previous day data for CPR
            cprs = {}
            hvs = {}
            atrs = {}
            for sym, daily_df in daily_data_dict.items():
                prev_days = daily_df[daily_df['time'].apply(lambda x: x.date()) < trade_date]
                if len(prev_days) == 0:
                    continue
                prev = prev_days.iloc[0]  # Most recent (data is reverse sorted)
                cprs[sym] = compute_cpr(prev['h'], prev['l'], prev['c'])
                hvs[sym] = compute_hv(prev_days) if len(prev_days) > 5 else 0.20
                atrs[sym] = compute_atr(prev_days)
                print(f"  {sym}: CPR BC={cprs[sym]['bc']:.0f} Pivot={cprs[sym]['pivot']:.0f} TC={cprs[sym]['tc']:.0f} "
                      f"Width={cprs[sym]['cpr_width']:.3f}% ATR={atrs[sym]:.0f} HV={hvs[sym]:.1%}")

            # Compute DTE (days to next expiry)
            dow = trade_date.weekday()  # 0=Mon ... 6=Sun
            # For simplicity: Thursday expiry for NIFTY/SENSEX, Wednesday for BANKNIFTY
            dte_map = {}
            for sym in spot_data_dict.keys():
                if sym == 'NIFTY BANK':
                    exp_dow = 2  # Wednesday
                else:
                    exp_dow = 3  # Thursday
                days_to_exp = (exp_dow - dow) % 7
                if days_to_exp == 0:
                    days_to_exp = 0.1  # Expiry day — near zero
                dte_map[sym] = days_to_exp

            # Track intraday OHLC
            intraday_ohlc = {}
            vwaps = {}
            vwap_sum_tp = {}
            vwap_count = {}

            scan_interval = 0  # Scan every minute for this backtest

            for bar_time in day_bars:
                # Only process during market hours
                if bar_time.time() < dtime(9, 16) or bar_time.time() > dtime(15, 30):
                    continue

                for sym, spot_df in spot_data_dict.items():
                    bar = spot_df[spot_df['time'] == bar_time]
                    if len(bar) == 0:
                        continue
                    bar = bar.iloc[0]
                    spot = bar['c']  # Use close of 1-min bar

                    # Update intraday OHLC
                    if sym not in intraday_ohlc:
                        intraday_ohlc[sym] = {'open': bar['o'], 'high': bar['h'], 'low': bar['l'], 'close': spot}
                        vwap_sum_tp[sym] = 0
                        vwap_count[sym] = 0
                    else:
                        intraday_ohlc[sym]['high'] = max(intraday_ohlc[sym]['high'], bar['h'])
                        intraday_ohlc[sym]['low'] = min(intraday_ohlc[sym]['low'], bar['l'])
                        intraday_ohlc[sym]['close'] = spot

                    # VWAP (typical price average since no volume for index)
                    tp = (bar['h'] + bar['l'] + bar['c']) / 3
                    vwap_sum_tp[sym] += tp
                    vwap_count[sym] += 1
                    vwaps[sym] = vwap_sum_tp[sym] / vwap_count[sym]

                    # Check exits first
                    self.check_exits(spot, bar_time)

                    # Check entries (every minute)
                    if sym in cprs:
                        signals = self.check_cpr_entry(
                            sym.replace('NIFTY 50', 'NIFTY').replace('NIFTY BANK', 'BANKNIFTY'),
                            spot, cprs[sym], intraday_ohlc[sym], hvs[sym], atrs[sym],
                            dte_map[sym], bar_time, vwaps.get(sym, spot)
                        )

                        for sig in signals:
                            can, why = self.can_enter(sym, bar_time, sig['type'])
                            if can:
                                pos = self.enter_trade(
                                    sym, sig, bar_time, spot, dte_map[sym]
                                )
                                print(f"  ENTRY {bar_time.strftime('%H:%M')} | {sym} {sig['type']} "
                                      f"Strike={sig['strike']:.0f} Prem={sig['premium']:.2f} "
                                      f"IV={sig['iv']:.1%} DTE={dte_map[sym]:.1f} | {sig['reason']}")

            # Force close any remaining at EOD
            for pos in list(self.positions):
                if pos.exit_time is None:
                    pos.close(pos.current_premium, datetime.combine(trade_date, dtime(15, 30)), 'EOD_FORCE_CLOSE')
                    self.closed_trades.append(pos)
                    self.positions.remove(pos)
                    day_key = trade_date.strftime('%Y-%m-%d')
                    self.daily_pnl[day_key] = self.daily_pnl.get(day_key, 0) + pos.pnl
                    self.capital += pos.pnl

            day_key = trade_date.strftime('%Y-%m-%d')
            day_pnl = self.daily_pnl.get(day_key, 0)
            print(f"\n  Day PnL: Rs {day_pnl:+,.0f} | Capital: Rs {self.capital:,.0f}")

        self.print_report()

    def print_report(self):
        """Print detailed trade-by-trade report"""
        print(f"\n{'='*120}")
        print(f"DETAILED TRADE REPORT")
        print(f"{'='*120}")
        print(f"{'#':>3} | {'Symbol':<12} | {'Type':<12} | {'Strike':>8} | {'Entry Time':<16} | {'Entry':>8} | "
              f"{'Exit Time':<16} | {'Exit':>8} | {'Hold':>8} | {'PnL':>10} | {'Exit Reason':<35} | {'IV':>5} | {'DTE':>4}")
        print('-' * 170)

        total_pnl = 0
        winners = 0
        losers = 0
        theta_kills = 0
        breakout_fails = 0

        for i, trade in enumerate(self.closed_trades, 1):
            hold_mins = (trade.exit_time - trade.entry_time).total_seconds() / 60
            hold_str = f"{int(hold_mins)}m"
            pnl_str = f"Rs {trade.pnl:+,.0f}"
            total_pnl += trade.pnl

            if trade.pnl > 0:
                winners += 1
            else:
                losers += 1

            if 'THETA' in (trade.exit_reason or ''):
                theta_kills += 1
            if 'BREAKOUT_FAIL' in (trade.exit_reason or ''):
                breakout_fails += 1

            sym_display = trade.symbol.replace('NIFTY 50', 'NIFTY').replace('NIFTY BANK', 'BANKNIFTY')

            print(f"{i:>3} | {sym_display:<12} | {trade.signal_type:<12} | {trade.strike:>8.0f} | "
                  f"{trade.entry_time.strftime('%m-%d %H:%M'):<16} | {trade.entry_premium:>8.2f} | "
                  f"{trade.exit_time.strftime('%m-%d %H:%M'):<16} | {trade.exit_premium:>8.2f} | "
                  f"{hold_str:>8} | {pnl_str:>10} | {(trade.exit_reason or '')[:35]:<35} | "
                  f"{trade.iv:>4.0%} | {trade.dte:>4.1f}")

        print('-' * 170)
        win_rate = winners / max(winners + losers, 1) * 100

        print(f"\nSUMMARY:")
        print(f"  Total Trades: {len(self.closed_trades)}")
        print(f"  Winners: {winners} | Losers: {losers} | Win Rate: {win_rate:.1f}%")
        print(f"  Theta Kills: {theta_kills} | Breakout Fails: {breakout_fails}")
        print(f"  Total PnL: Rs {total_pnl:+,.0f}")
        print(f"  Final Capital: Rs {self.capital:,.0f} (started Rs {INITIAL_CAPITAL:,.0f})")

        # ---- WHAT-IF ANALYSIS: Without THETA_DECAY_EXIT ----
        theta_trades = [t for t in self.closed_trades if 'THETA' in (t.exit_reason or '')]
        if theta_trades:
            theta_loss = sum(t.pnl for t in theta_trades)
            print(f"\n  WHAT-IF: Without THETA_DECAY_EXIT:")
            print(f"    Theta exit losses avoided: Rs {theta_loss:+,.0f}")
            print(f"    Those trades would have continued to EOD")

        # PnL breakdown by exit reason
        print(f"\n  PnL BY EXIT REASON:")
        reason_pnl = {}
        for t in self.closed_trades:
            r = (t.exit_reason or 'UNKNOWN').split(' ')[0]
            if r not in reason_pnl:
                reason_pnl[r] = {'count': 0, 'pnl': 0}
            reason_pnl[r]['count'] += 1
            reason_pnl[r]['pnl'] += t.pnl

        for r, data in sorted(reason_pnl.items(), key=lambda x: x[1]['pnl']):
            print(f"    {r:<30} | {data['count']:>3} trades | Rs {data['pnl']:>+10,.0f}")

        return {
            'total_trades': len(self.closed_trades),
            'winners': winners,
            'losers': losers,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'theta_kills': theta_kills,
            'breakout_fails': breakout_fails,
        }


def main():
    """Fetch TrueData data and run backtest"""
    from truedata_ws.websocket.TD import TD
    import time

    print("Connecting to TrueData...")
    td = TD('Trial138', 'rambabu138', live_port=8086)
    time.sleep(3)

    symbols = ['NIFTY 50', 'NIFTY BANK']

    # Fetch 1-min bars (covers ~14 trading days)
    spot_data = {}
    for sym in symbols:
        print(f"Fetching 1-min bars for {sym}...")
        bars = td.get_n_historical_bars(sym, no_of_bars=3000, bar_size='1 min')
        df = pd.DataFrame(bars) if not isinstance(bars, pd.DataFrame) else bars
        df['time'] = pd.to_datetime(df['time'])
        spot_data[sym] = df.sort_values('time').reset_index(drop=True)
        print(f"  Got {len(df)} bars from {df['time'].min()} to {df['time'].max()}")

    # Fetch daily (EOD) bars for CPR calculation
    daily_data = {}
    for sym in symbols:
        print(f"Fetching EOD bars for {sym}...")
        bars = td.get_n_historical_bars(sym, no_of_bars=100, bar_size='EOD')
        df = pd.DataFrame(bars) if not isinstance(bars, pd.DataFrame) else bars
        df['time'] = pd.to_datetime(df['time'])
        daily_data[sym] = df.sort_values('time', ascending=False).reset_index(drop=True)
        print(f"  Got {len(df)} daily bars from {df['time'].min()} to {df['time'].max()}")

    td.disconnect()
    print("TrueData disconnected.\n")

    # ====================================================================
    # RUN 1: WITH THETA_DECAY_EXIT (current live behavior)
    # ====================================================================
    bt1 = TickByTickBacktest()
    bt1.run_backtest(spot_data, daily_data, "RUN 1: WITH THETA_DECAY_EXIT (Current Live System)")

    # ====================================================================
    # RUN 2: WITHOUT THETA_DECAY_EXIT (proposed fix)
    # ====================================================================
    # Temporarily disable theta exit
    global THETA_BURDEN_EXIT_PCT
    old_theta = THETA_BURDEN_EXIT_PCT
    THETA_BURDEN_EXIT_PCT = 999  # Effectively disable

    bt2 = TickByTickBacktest()
    bt2.run_backtest(spot_data, daily_data, "RUN 2: WITHOUT THETA_DECAY_EXIT (Proposed Fix)")

    THETA_BURDEN_EXIT_PCT = old_theta

    # ====================================================================
    # RUN 3: WITHOUT BREAKOUT_FAIL (let trades run)
    # ====================================================================
    global BREAKOUT_FAIL_CHECK_MINUTES
    old_bf = BREAKOUT_FAIL_CHECK_MINUTES
    BREAKOUT_FAIL_CHECK_MINUTES = 0
    THETA_BURDEN_EXIT_PCT = 999

    bt3 = TickByTickBacktest()
    bt3.run_backtest(spot_data, daily_data, "RUN 3: NO THETA + NO BREAKOUT_FAIL (Let Winners Run)")

    BREAKOUT_FAIL_CHECK_MINUTES = old_bf
    THETA_BURDEN_EXIT_PCT = old_theta

    # ====================================================================
    # COMPARISON TABLE
    # ====================================================================
    print(f"\n{'='*90}")
    print(f"COMPARISON: 3 SCENARIOS")
    print(f"{'='*90}")
    print(f"{'Scenario':<50} | {'Trades':>6} | {'WinRate':>7} | {'PnL':>12} | {'Theta':>6} | {'BF':>4}")
    print('-' * 90)

    for label, bt in [
        ("1. Current System (with bugs)", bt1),
        ("2. Fix: Remove THETA_DECAY_EXIT", bt2),
        ("3. Fix: Remove THETA + BREAKOUT_FAIL", bt3),
    ]:
        ct = len(bt.closed_trades)
        w = sum(1 for t in bt.closed_trades if t.pnl > 0)
        wr = w/max(ct,1)*100
        pnl = sum(t.pnl for t in bt.closed_trades)
        tk = sum(1 for t in bt.closed_trades if 'THETA' in (t.exit_reason or ''))
        bf = sum(1 for t in bt.closed_trades if 'BREAKOUT' in (t.exit_reason or ''))
        print(f"{label:<50} | {ct:>6} | {wr:>6.1f}% | Rs {pnl:>+9,.0f} | {tk:>6} | {bf:>4}")

    print(f"\nBacktest complete. All data from TrueData real 1-min bars.")


if __name__ == '__main__':
    main()
