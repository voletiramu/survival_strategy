"""
OI (Open Interest) Velocity Monitor - Based on Raahi Bhushan's Gamma Blast Detection
========================================================================================
This module monitors OI changes every minute for ATM and near-ATM strikes.
Designed for LIVE trading with Zerodha Kite Connect API.

Architecture:
- Fetches OI data every minute via Kite Connect historical_data API
- Tracks 5 strikes: ATM-2, ATM-1, ATM, ATM+1, ATM+2
- Separate tables for CE and PE
- Columns: 10min OI change%, 15min OI change%, 30min OI change%
- Color codes cells when thresholds breached
- Triggers alerts when >50% of cells are flagged

REQUIRES: pip install kiteconnect
You need Kite Connect API credentials (api_key, access_token).

DISCLAIMER: For educational purposes. Not financial advice.
"""

import time
import datetime
import pandas as pd
import numpy as np
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class OISnapshot:
    """Single OI reading for a strike."""
    timestamp: datetime.datetime
    strike: int
    option_type: str  # 'CE' or 'PE'
    oi: int
    volume: int
    ltp: float


@dataclass
class OIAlert:
    """Alert generated when OI velocity exceeds threshold."""
    timestamp: datetime.datetime
    strike: int
    option_type: str
    window_minutes: int
    oi_change_pct: float
    direction: str  # 'BUILDUP' or 'UNWINDING'
    severity: str  # 'LOW', 'MEDIUM', 'HIGH', 'GAMMA_BLAST'


class OIVelocityMonitor:
    """
    Real-time OI velocity monitor.

    Thresholds (per Raahi Bhushan):
    - >10% OI change in 10 minutes
    - >15% OI change in 15 minutes
    - >25% OI change in 30 minutes
    """

    # Alert thresholds
    THRESHOLDS = {
        10: 0.10,  # 10% in 10 min
        15: 0.15,  # 15% in 15 min
        30: 0.25,  # 25% in 30 min
    }

    GAMMA_BLAST_THRESHOLD = 0.50  # >50% of cells flagged = gamma blast warning

    def __init__(self, symbol: str = "BANKNIFTY", strike_range: int = 2,
                 strike_gap: int = 100, history_minutes: int = 60):
        """
        Args:
            symbol: Index symbol
            strike_range: Number of strikes above and below ATM
            strike_gap: Gap between strikes (100 for BankNifty, 50 for Nifty)
            history_minutes: Minutes of OI history to keep
        """
        self.symbol = symbol
        self.strike_range = strike_range
        self.strike_gap = strike_gap
        self.history_minutes = history_minutes

        # OI history: {(strike, option_type): deque of OISnapshot}
        self.oi_history: Dict[Tuple[int, str], deque] = {}
        self.alerts: List[OIAlert] = []
        self.kite = None

    def connect_kite(self, api_key: str, access_token: str):
        """Connect to Kite Connect API."""
        try:
            from kiteconnect import KiteConnect
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)
            print(f"Connected to Kite Connect for {self.symbol}")
        except ImportError:
            print("kiteconnect not installed. Run: pip install kiteconnect")
            print("Using simulation mode.")
            self.kite = None

    def get_atm_strike(self, spot_price: float) -> int:
        """Get ATM strike price."""
        return round(spot_price / self.strike_gap) * self.strike_gap

    def get_monitored_strikes(self, spot_price: float) -> List[int]:
        """Get list of strikes to monitor: ATM-2, ATM-1, ATM, ATM+1, ATM+2."""
        atm = self.get_atm_strike(spot_price)
        strikes = []
        for offset in range(-self.strike_range, self.strike_range + 1):
            strikes.append(atm + offset * self.strike_gap)
        return strikes

    def fetch_oi_snapshot(self, spot_price: float) -> List[OISnapshot]:
        """
        Fetch current OI for all monitored strikes.
        Uses Kite Connect historical_data API or quote API.
        """
        snapshots = []
        strikes = self.get_monitored_strikes(spot_price)
        now = datetime.datetime.now()

        if self.kite is None:
            # Simulation mode - generate random OI
            for strike in strikes:
                for opt_type in ['CE', 'PE']:
                    base_oi = np.random.randint(50000, 500000)
                    snapshots.append(OISnapshot(
                        timestamp=now, strike=strike,
                        option_type=opt_type, oi=base_oi,
                        volume=np.random.randint(1000, 50000),
                        ltp=np.random.uniform(10, 500)
                    ))
            return snapshots

        # Real Kite Connect fetch
        for strike in strikes:
            for opt_type in ['CE', 'PE']:
                try:
                    # Construct instrument token
                    # You need to look up instrument tokens from Kite instruments dump
                    expiry = self._get_current_expiry()
                    tradingsymbol = f"{self.symbol}{expiry}{strike}{opt_type}"

                    # Using quote API for real-time OI
                    quote = self.kite.quote(f"NFO:{tradingsymbol}")
                    if quote:
                        data = list(quote.values())[0]
                        snapshots.append(OISnapshot(
                            timestamp=now,
                            strike=strike,
                            option_type=opt_type,
                            oi=data.get('oi', 0),
                            volume=data.get('volume', 0),
                            ltp=data.get('last_price', 0)
                        ))
                except Exception as e:
                    print(f"Error fetching {strike}{opt_type}: {e}")

        return snapshots

    def _get_current_expiry(self) -> str:
        """Get current week's expiry date string (e.g., '24FEB')."""
        today = datetime.date.today()
        # Find next Wednesday (BankNifty) or Thursday (Nifty)
        target_day = 2 if 'BANK' in self.symbol else 3
        days_ahead = target_day - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        expiry = today + datetime.timedelta(days=days_ahead)
        return expiry.strftime('%y%b').upper()

    def update(self, spot_price: float) -> List[OIAlert]:
        """
        Main update function. Call every minute.
        Returns list of new alerts.
        """
        snapshots = self.fetch_oi_snapshot(spot_price)
        new_alerts = []

        for snap in snapshots:
            key = (snap.strike, snap.option_type)

            # Initialize history if needed
            if key not in self.oi_history:
                self.oi_history[key] = deque(maxlen=self.history_minutes)

            self.oi_history[key].append(snap)

            # Check thresholds for each window
            for window_min, threshold in self.THRESHOLDS.items():
                alert = self._check_oi_velocity(key, window_min, threshold)
                if alert:
                    new_alerts.append(alert)
                    self.alerts.append(alert)

        # Check for gamma blast condition
        if self._check_gamma_blast(new_alerts):
            blast_alert = OIAlert(
                timestamp=datetime.datetime.now(),
                strike=0, option_type='ALL',
                window_minutes=0,
                oi_change_pct=0,
                direction='GAMMA_BLAST',
                severity='GAMMA_BLAST'
            )
            new_alerts.append(blast_alert)
            self.alerts.append(blast_alert)

        return new_alerts

    def _check_oi_velocity(self, key: Tuple[int, str],
                            window_minutes: int,
                            threshold: float) -> Optional[OIAlert]:
        """Check if OI change exceeds threshold for given window."""
        history = self.oi_history.get(key, deque())

        if len(history) < window_minutes:
            return None

        current_oi = history[-1].oi
        past_oi = history[-window_minutes].oi

        if past_oi == 0:
            return None

        oi_change_pct = (current_oi - past_oi) / past_oi

        if abs(oi_change_pct) >= threshold:
            direction = 'BUILDUP' if oi_change_pct > 0 else 'UNWINDING'

            # Severity based on how much it exceeds threshold
            ratio = abs(oi_change_pct) / threshold
            if ratio > 3:
                severity = 'HIGH'
            elif ratio > 2:
                severity = 'MEDIUM'
            else:
                severity = 'LOW'

            return OIAlert(
                timestamp=datetime.datetime.now(),
                strike=key[0],
                option_type=key[1],
                window_minutes=window_minutes,
                oi_change_pct=oi_change_pct * 100,
                direction=direction,
                severity=severity
            )

        return None

    def _check_gamma_blast(self, current_alerts: List[OIAlert]) -> bool:
        """
        Check if gamma blast condition is met.
        Per Raahi: >50% of cells color coded = gamma blast warning.
        """
        total_cells = (2 * self.strike_range + 1) * 2 * len(self.THRESHOLDS)
        flagged_cells = len(current_alerts)

        if total_cells > 0:
            return flagged_cells / total_cells > self.GAMMA_BLAST_THRESHOLD
        return False

    def get_oi_table(self) -> pd.DataFrame:
        """
        Generate the OI velocity table (like Raahi's 5x3 tables).
        Returns DataFrame with strikes as rows, time windows as columns.
        """
        rows = []
        for key, history in self.oi_history.items():
            if len(history) < 2:
                continue

            strike, opt_type = key
            current_oi = history[-1].oi

            row = {
                'Strike': strike,
                'Type': opt_type,
                'Current OI': current_oi,
            }

            for window in [10, 15, 30]:
                if len(history) >= window:
                    past_oi = history[-window].oi
                    if past_oi > 0:
                        change = (current_oi - past_oi) / past_oi * 100
                        row[f'{window}min %'] = f"{change:.1f}%"
                        row[f'{window}min_flag'] = abs(change) >= self.THRESHOLDS[window] * 100
                    else:
                        row[f'{window}min %'] = 'N/A'
                        row[f'{window}min_flag'] = False
                else:
                    row[f'{window}min %'] = 'Wait'
                    row[f'{window}min_flag'] = False

            rows.append(row)

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        df = df.sort_values(['Type', 'Strike'])
        return df

    def get_signal(self) -> dict:
        """
        Get trading signal based on OI analysis.
        Returns: {'direction': 'UP'/'DOWN'/'NEUTRAL', 'strength': 0-1, 'gamma_blast': bool}
        """
        recent_alerts = [a for a in self.alerts
                        if (datetime.datetime.now() - a.timestamp).seconds < 300]

        if not recent_alerts:
            return {'direction': 'NEUTRAL', 'strength': 0, 'gamma_blast': False}

        ce_buildup = sum(1 for a in recent_alerts
                        if a.option_type == 'CE' and a.direction == 'BUILDUP')
        pe_buildup = sum(1 for a in recent_alerts
                        if a.option_type == 'PE' and a.direction == 'BUILDUP')

        gamma_blast = any(a.severity == 'GAMMA_BLAST' for a in recent_alerts)

        # PE buildup = bullish (writers are selling puts = bullish)
        # CE buildup = bearish (writers are selling calls = bearish)
        # But for gamma blast, buildup in CE = big players buying calls = bullish
        if gamma_blast:
            if ce_buildup > pe_buildup:
                direction = 'UP'
            elif pe_buildup > ce_buildup:
                direction = 'DOWN'
            else:
                direction = 'NEUTRAL'
        else:
            if pe_buildup > ce_buildup:
                direction = 'UP'
            elif ce_buildup > pe_buildup:
                direction = 'DOWN'
            else:
                direction = 'NEUTRAL'

        total = max(ce_buildup + pe_buildup, 1)
        strength = abs(ce_buildup - pe_buildup) / total

        return {
            'direction': direction,
            'strength': strength,
            'gamma_blast': gamma_blast,
            'ce_buildup': ce_buildup,
            'pe_buildup': pe_buildup,
            'total_alerts': len(recent_alerts)
        }


# === LIVE TRADING LOOP EXAMPLE ===
def run_live_oi_monitor(api_key: str = "", access_token: str = "",
                         symbol: str = "BANKNIFTY", spot_price: float = 48000):
    """
    Example live OI monitoring loop.
    Run this during market hours (9:15 AM - 3:30 PM).
    """
    monitor = OIVelocityMonitor(
        symbol=symbol,
        strike_range=2,
        strike_gap=100 if 'BANK' in symbol else 50
    )

    if api_key and access_token:
        monitor.connect_kite(api_key, access_token)

    print(f"OI Velocity Monitor started for {symbol}")
    print(f"Monitoring strikes around {spot_price}")
    print("=" * 60)

    while True:
        try:
            alerts = monitor.update(spot_price)

            # Print OI table
            table = monitor.get_oi_table()
            if not table.empty:
                print(f"\n--- {datetime.datetime.now().strftime('%H:%M:%S')} ---")
                print(table.to_string(index=False))

            # Print alerts
            for alert in alerts:
                flag = "!!!" if alert.severity in ['HIGH', 'GAMMA_BLAST'] else "!"
                print(f"{flag} ALERT: {alert.strike} {alert.option_type} "
                      f"{alert.direction} {alert.oi_change_pct:.1f}% "
                      f"in {alert.window_minutes}min [{alert.severity}]")

            # Get signal
            signal = monitor.get_signal()
            if signal['gamma_blast']:
                print(f"\n*** GAMMA BLAST DETECTED! Direction: {signal['direction']} ***")
            elif signal['direction'] != 'NEUTRAL':
                print(f"Signal: {signal['direction']} (strength: {signal['strength']:.2f})")

            # Wait 60 seconds
            time.sleep(60)

        except KeyboardInterrupt:
            print("\nOI Monitor stopped.")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    # Run in simulation mode (no Kite API needed)
    run_live_oi_monitor(symbol="BANKNIFTY", spot_price=48000)
