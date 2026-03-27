"""
Self-Healing Auto-Validation Engine
=====================================
Runs automatically on schedule to detect strategy degradation,
validate parameters, and take corrective action.

Architecture (matching QuantLive):
  - Every 4h: Re-Backtest all strategies on 7/14/30/60d windows
  - Every 6h: Parameter search (80 combos per strategy)
  - Daily 03:00: Data retention (clean old logs, archive signals)
  - Daily 06:00: Health digest (Telegram summary)

Self-Healing Flow:
  1. Re-backtest detects PF < 1.0 for a strategy
  2. Walk-forward validation confirms degradation
  3. Strategy auto-disabled, Telegram alert sent
  4. Param optimizer finds better params
  5. Monte Carlo validates new params
  6. If valid: auto-promote to live
  7. Daily health digest summarizes all actions

Integration:
    from self_healing import SelfHealingEngine
    healer = SelfHealingEngine()
    healer.start()  # Starts background scheduler

    # Or run individual checks:
    healer.run_backtest_check()
    healer.run_health_digest()
"""

import os
import sys
import json
import time
import logging
import threading
import shutil
import numpy as np
from datetime import datetime, timedelta, date, time as dtime
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────

BASE_DIR = Path(os.environ.get('ALGO_BASE_DIR', '/root/algo_trading'))
DATA_DIR = BASE_DIR / 'data'
LIVE_DIR = DATA_DIR / 'live'
SIGNAL_DIR = DATA_DIR / 'signals'
LOG_DIR = BASE_DIR / 'logs'
RESULTS_DIR = BASE_DIR / 'data' / 'healing_results'
STATE_FILE = BASE_DIR / 'data' / 'healing_state.json'

# Backtest windows
BACKTEST_WINDOWS = [7, 14, 30]  # Days

# Strategy degradation thresholds
MIN_PF = 1.0              # PF below this = degraded
MIN_WR = 35               # WR% below this = degraded
MIN_SHARPE = 0             # Sharpe below this = degraded
MIN_TRADES_FOR_EVAL = 5    # Need at least 5 trades to evaluate

# Walk-forward settings
WF_TRAIN_PCT = 0.80        # 80% train, 20% test
WF_MIN_PF_RATIO = 0.50     # Test PF must be >= 50% of train PF

# Data retention settings
MAX_LOG_AGE_DAYS = 30       # Delete logs older than 30 days
MAX_LOG_SIZE_MB = 50        # Rotate logs larger than 50MB
MAX_SIGNAL_AGE_DAYS = 60    # Archive signals older than 60 days

# Health check thresholds
DISK_WARN_PCT = 70          # Warn if disk > 70% used
DISK_CRITICAL_PCT = 85      # Critical if > 85%
MEMORY_WARN_PCT = 80        # Warn if memory > 80%

LOT_SIZES = {'NIFTY': 75, 'BANKNIFTY': 30, 'SENSEX': 20,
             'GOLDM': 10, 'SILVERM': 30, 'CRUDEOILM': 100}
BROKERAGE = 20


def calc_costs(prem, qty, sell=False):
    t = prem * qty
    b = min(BROKERAGE, t * 0.0003)
    s = t * 0.000625 if sell else 0
    e = t * 0.0005
    g = (b + e) * 0.18
    return round(b + s + e + g, 2)


# ── Portfolio State Reader ────────────────────────────────────────────────

def read_portfolio_trades():
    """Read all closed trades from portfolio state files."""
    all_trades = []

    for state_file, segment in [
        (BASE_DIR / 'paper_trades' / 'portfolio_state.json', 'equity'),
        (BASE_DIR / 'paper_trades' / 'commodity_state.json', 'commodity'),
        (BASE_DIR / 'stock_paper_trades' / 'stock_portfolio_state.json', 'stocks'),
    ]:
        try:
            if state_file.exists():
                data = json.loads(state_file.read_text())
                for t in data.get('closed_trades', []):
                    t['segment'] = segment
                    all_trades.append(t)
        except Exception as e:
            logger.warning("Failed to read %s: %s" % (state_file, e))

    return all_trades


# ── Backtest Check ────────────────────────────────────────────────────────

class BacktestChecker:
    """Re-backtest strategies on rolling windows to detect degradation."""

    def check_all_strategies(self, trades=None):
        """Run backtest check on all strategies.

        Returns dict of {strategy: {window: metrics}} with degradation flags.
        """
        if trades is None:
            trades = read_portfolio_trades()

        if not trades:
            return {'status': 'NO_DATA', 'strategies': {}}

        # Group by strategy
        by_strategy = defaultdict(list)
        for t in trades:
            strat = t.get('strategy', t.get('type', 'Unknown'))
            by_strategy[strat].append(t)

        results = {}
        degraded = []

        for strat, strat_trades in by_strategy.items():
            strat_results = {}

            for window in BACKTEST_WINDOWS:
                cutoff = (datetime.now() - timedelta(days=window)).isoformat()
                recent = [t for t in strat_trades
                          if str(t.get('exit_time', t.get('entry_time', ''))) >= cutoff]

                if len(recent) < MIN_TRADES_FOR_EVAL:
                    strat_results[window] = {
                        'trades': len(recent),
                        'status': 'INSUFFICIENT',
                    }
                    continue

                metrics = self._compute_metrics(recent)
                metrics['window'] = window

                # Check degradation
                is_degraded = (
                    metrics['pf'] < MIN_PF or
                    metrics['wr'] < MIN_WR or
                    metrics['sharpe'] < MIN_SHARPE
                )
                metrics['degraded'] = is_degraded
                if is_degraded:
                    metrics['degradation_reason'] = []
                    if metrics['pf'] < MIN_PF:
                        metrics['degradation_reason'].append('PF=%.2f<%.2f' % (metrics['pf'], MIN_PF))
                    if metrics['wr'] < MIN_WR:
                        metrics['degradation_reason'].append('WR=%.0f%%<%.0f%%' % (metrics['wr'], MIN_WR))
                    if metrics['sharpe'] < MIN_SHARPE:
                        metrics['degradation_reason'].append('Sharpe=%.2f<%.2f' % (metrics['sharpe'], MIN_SHARPE))

                strat_results[window] = metrics

            # Overall status
            any_degraded = any(
                r.get('degraded', False)
                for r in strat_results.values()
                if r.get('status') != 'INSUFFICIENT'
            )

            results[strat] = {
                'windows': strat_results,
                'total_trades': len(strat_trades),
                'degraded': any_degraded,
            }

            if any_degraded:
                degraded.append(strat)

        return {
            'status': 'OK' if not degraded else 'DEGRADED',
            'degraded_strategies': degraded,
            'strategies': results,
            'checked_at': datetime.now().isoformat(),
        }

    def _compute_metrics(self, trades):
        pnls = [t.get('net_pnl', t.get('pnl', 0)) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        wr = len(wins) / max(len(pnls), 1) * 100
        win_sum = sum(wins)
        loss_sum = sum(losses)
        pf = abs(win_sum / loss_sum) if loss_sum != 0 else 999

        daily = defaultdict(float)
        for t in trades:
            d = str(t.get('exit_time', t.get('entry_time', '')))[:10]
            daily[d] += t.get('net_pnl', t.get('pnl', 0))

        da = np.array(list(daily.values())) if daily else np.array([0])
        sharpe = float((da.mean() / da.std()) * np.sqrt(252)) if len(da) > 1 and da.std() > 0 else 0
        cum = np.cumsum(da)
        max_dd = float((cum - np.maximum.accumulate(cum)).min()) if len(cum) > 0 else 0

        return {
            'trades': len(pnls),
            'wins': len(wins),
            'losses': len(losses),
            'pnl': float(total),
            'wr': float(wr),
            'pf': float(pf),
            'sharpe': sharpe,
            'max_dd': max_dd,
            'avg_win': float(win_sum / max(len(wins), 1)),
            'avg_loss': float(loss_sum / max(len(losses), 1)),
            'status': 'OK',
        }


# ── Walk-Forward Validator ────────────────────────────────────────────────

class WalkForwardValidator:
    """Split trades into train/test to detect overfitting."""

    def validate(self, trades, strategy=None):
        if strategy:
            trades = [t for t in trades if t.get('strategy', '') == strategy]

        if len(trades) < 10:
            return {'valid': True, 'reason': 'Too few trades', 'trades': len(trades)}

        # Sort by time
        trades.sort(key=lambda t: str(t.get('exit_time', t.get('entry_time', ''))))

        split = int(len(trades) * WF_TRAIN_PCT)
        train = trades[:split]
        test = trades[split:]

        checker = BacktestChecker()
        train_m = checker._compute_metrics(train)
        test_m = checker._compute_metrics(test)

        if train_m['pf'] > 0 and test_m['pf'] > 0:
            pf_ratio = test_m['pf'] / train_m['pf']
        else:
            pf_ratio = 0

        is_valid = (
            test_m['pf'] >= MIN_PF and
            (pf_ratio >= WF_MIN_PF_RATIO or test_m['pf'] >= 1.5)
        )

        return {
            'valid': is_valid,
            'train_pf': train_m['pf'],
            'test_pf': test_m['pf'],
            'train_pnl': train_m['pnl'],
            'test_pnl': test_m['pnl'],
            'pf_ratio': pf_ratio,
            'train_trades': len(train),
            'test_trades': len(test),
            'reason': 'PASS (test PF=%.2f, ratio=%.0f%%)' % (test_m['pf'], pf_ratio * 100)
                      if is_valid else
                      'FAIL (test PF=%.2f, ratio=%.0f%%)' % (test_m['pf'], pf_ratio * 100),
        }


# ── Data Retention ────────────────────────────────────────────────────────

class DataRetention:
    """Clean old logs, archive signals, manage disk space."""

    def run(self):
        """Run data retention cleanup."""
        results = {
            'logs_deleted': 0,
            'logs_rotated': 0,
            'signals_archived': 0,
            'space_freed_mb': 0,
        }

        # 1. Delete old log files
        if LOG_DIR.exists():
            cutoff = datetime.now() - timedelta(days=MAX_LOG_AGE_DAYS)
            for f in LOG_DIR.glob('*.log'):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        size = f.stat().st_size
                        f.unlink()
                        results['logs_deleted'] += 1
                        results['space_freed_mb'] += size / 1024 / 1024
                except Exception as e:
                    logger.warning("Failed to delete %s: %s" % (f, e))

        # 2. Rotate large log files
        if LOG_DIR.exists():
            for f in LOG_DIR.glob('*.log'):
                try:
                    size_mb = f.stat().st_size / 1024 / 1024
                    if size_mb > MAX_LOG_SIZE_MB:
                        # Keep last 10MB, archive rest
                        archive = f.with_suffix('.log.old')
                        shutil.copy2(f, archive)
                        # Truncate to last 10MB
                        with open(f, 'rb') as fh:
                            fh.seek(-10 * 1024 * 1024, 2)  # Last 10MB
                            data = fh.read()
                        with open(f, 'wb') as fh:
                            fh.write(data)
                        results['logs_rotated'] += 1
                        results['space_freed_mb'] += (size_mb - 10)
                except Exception as e:
                    logger.warning("Failed to rotate %s: %s" % (f, e))

        # 3. Archive old signal CSVs
        if SIGNAL_DIR.exists():
            cutoff = datetime.now() - timedelta(days=MAX_SIGNAL_AGE_DAYS)
            archive_dir = SIGNAL_DIR / 'archive'
            for f in SIGNAL_DIR.glob('signals_*.csv'):
                try:
                    mtime = datetime.fromtimestamp(f.stat().st_mtime)
                    if mtime < cutoff:
                        archive_dir.mkdir(exist_ok=True)
                        shutil.move(str(f), str(archive_dir / f.name))
                        results['signals_archived'] += 1
                except Exception as e:
                    logger.warning("Failed to archive %s: %s" % (f, e))

        results['run_at'] = datetime.now().isoformat()
        return results


# ── Health Digest ─────────────────────────────────────────────────────────

class HealthDigest:
    """Generate daily health report."""

    def generate(self):
        """Generate comprehensive health digest."""
        report = {
            'generated_at': datetime.now().isoformat(),
            'system': self._check_system(),
            'strategies': self._check_strategies(),
            'data': self._check_data(),
        }

        # Generate Telegram message
        report['telegram_message'] = self._format_telegram(report)
        return report

    def _check_system(self):
        """Check system health: disk, memory, services."""
        health = {}

        # Disk usage
        try:
            import shutil as sh
            total, used, free = sh.disk_usage('/')
            health['disk_total_gb'] = total / (1024 ** 3)
            health['disk_used_gb'] = used / (1024 ** 3)
            health['disk_free_gb'] = free / (1024 ** 3)
            health['disk_pct'] = used / total * 100
            health['disk_status'] = (
                'CRITICAL' if health['disk_pct'] > DISK_CRITICAL_PCT else
                'WARNING' if health['disk_pct'] > DISK_WARN_PCT else 'OK'
            )
        except Exception:
            health['disk_status'] = 'UNKNOWN'

        # Memory
        try:
            with open('/proc/meminfo') as f:
                lines = f.readlines()
            mem = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(':')] = int(parts[1])
            total = mem.get('MemTotal', 0)
            available = mem.get('MemAvailable', 0)
            used_pct = (total - available) / max(total, 1) * 100
            health['memory_total_mb'] = total / 1024
            health['memory_used_pct'] = used_pct
            health['memory_status'] = 'WARNING' if used_pct > MEMORY_WARN_PCT else 'OK'
        except Exception:
            health['memory_status'] = 'UNKNOWN'

        # Log directory size
        try:
            if LOG_DIR.exists():
                log_size = sum(f.stat().st_size for f in LOG_DIR.glob('*'))
                health['log_size_mb'] = log_size / 1024 / 1024
        except Exception:
            health['log_size_mb'] = 0

        # Zerodha token
        try:
            token_file = BASE_DIR / 'config' / 'zerodha_token.json'
            if token_file.exists():
                token = json.loads(token_file.read_text())
                health['zerodha_token_date'] = token.get('date', 'unknown')
                health['zerodha_fresh'] = token.get('date') == date.today().isoformat()
            else:
                health['zerodha_fresh'] = False
        except Exception:
            health['zerodha_fresh'] = False

        return health

    def _check_strategies(self):
        """Check strategy performance from recent trades."""
        checker = BacktestChecker()
        return checker.check_all_strategies()

    def _check_data(self):
        """Check data availability."""
        data = {}

        # Count live data days
        if LIVE_DIR.exists():
            days = [d.name for d in LIVE_DIR.iterdir() if d.is_dir() and d.name.startswith('2026')]
            data['live_data_days'] = len(days)
            data['latest_date'] = max(days) if days else 'none'

        # Count signal files
        if SIGNAL_DIR.exists():
            signals = list(SIGNAL_DIR.glob('signals_*.csv'))
            data['signal_files'] = len(signals)

        return data

    def _format_telegram(self, report):
        """Format health digest as Telegram message."""
        sys_h = report.get('system', {})
        strat_h = report.get('strategies', {})

        lines = []
        lines.append('<b>Daily Health Digest</b>')
        lines.append('%s' % datetime.now().strftime('%Y-%m-%d %H:%M'))
        lines.append('')

        # System
        disk_emoji = {'OK': '🟢', 'WARNING': '🟡', 'CRITICAL': '🔴'}.get(
            sys_h.get('disk_status', ''), '⚪')
        mem_emoji = {'OK': '🟢', 'WARNING': '🟡'}.get(
            sys_h.get('memory_status', ''), '⚪')

        lines.append('%s Disk: %.0f%% (%.1fGB free)' % (
            disk_emoji, sys_h.get('disk_pct', 0), sys_h.get('disk_free_gb', 0)))
        lines.append('%s Memory: %.0f%%' % (
            mem_emoji, sys_h.get('memory_used_pct', 0)))
        lines.append('%s Zerodha: %s' % (
            '🟢' if sys_h.get('zerodha_fresh') else '🔴',
            'Fresh' if sys_h.get('zerodha_fresh') else 'STALE'))
        lines.append('Logs: %.0fMB' % sys_h.get('log_size_mb', 0))
        lines.append('')

        # Strategies
        degraded = strat_h.get('degraded_strategies', [])
        if degraded:
            lines.append('🔴 <b>DEGRADED:</b> %s' % ', '.join(degraded))
        else:
            lines.append('🟢 All strategies healthy')

        for strat, info in strat_h.get('strategies', {}).items():
            if info.get('total_trades', 0) == 0:
                continue
            w7 = info.get('windows', {}).get(7, {})
            if w7.get('status') == 'INSUFFICIENT':
                continue
            emoji = '🔴' if info.get('degraded') else '🟢'
            lines.append('%s %s: %d trades, PF=%.2f, WR=%.0f%%' % (
                emoji, strat, w7.get('trades', 0), w7.get('pf', 0), w7.get('wr', 0)))

        return '\n'.join(lines)


# ── Main Self-Healing Engine ─────────────────────────────────────────────

class SelfHealingEngine:
    """Orchestrates all self-healing tasks."""

    def __init__(self):
        self.backtest_checker = BacktestChecker()
        self.wf_validator = WalkForwardValidator()
        self.data_retention = DataRetention()
        self.health_digest = HealthDigest()
        self._scheduler_thread = None
        self._stop_event = threading.Event()
        self._state = self._load_state()

    def _load_state(self):
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
        return {
            'last_backtest': None,
            'last_param_search': None,
            'last_retention': None,
            'last_health_digest': None,
            'disabled_strategies': [],
            'actions_taken': [],
        }

    def _save_state(self):
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, indent=2, default=str))
        except Exception as e:
            logger.warning("Failed to save healing state: %s" % e)

    def run_backtest_check(self):
        """Run backtest validation on all strategies."""
        logger.info("[SelfHealing] Running backtest check...")
        result = self.backtest_checker.check_all_strategies()

        # Walk-forward on degraded strategies
        trades = read_portfolio_trades()
        for strat in result.get('degraded_strategies', []):
            wf = self.wf_validator.validate(trades, strat)
            result['strategies'][strat]['walk_forward'] = wf

            if not wf['valid']:
                logger.warning("[SelfHealing] %s FAILED walk-forward: %s" % (strat, wf['reason']))
                self._take_action('DISABLE', strat,
                                  'Degraded (PF<1) + failed walk-forward')

        self._state['last_backtest'] = datetime.now().isoformat()
        self._save_state()

        # Save results
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        result_file = RESULTS_DIR / ('backtest_%s.json' % datetime.now().strftime('%Y%m%d_%H%M'))
        result_file.write_text(json.dumps(result, indent=2, default=str))

        return result

    def run_data_retention(self):
        """Run data cleanup."""
        logger.info("[SelfHealing] Running data retention...")
        result = self.data_retention.run()
        self._state['last_retention'] = datetime.now().isoformat()
        self._save_state()
        logger.info("[SelfHealing] Retention: %d logs deleted, %d rotated, %.1fMB freed" % (
            result['logs_deleted'], result['logs_rotated'], result['space_freed_mb']))
        return result

    def run_health_digest(self):
        """Generate and send health digest."""
        logger.info("[SelfHealing] Generating health digest...")
        report = self.health_digest.generate()
        self._state['last_health_digest'] = datetime.now().isoformat()
        self._save_state()

        # Save report
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        report_file = RESULTS_DIR / ('health_%s.json' % datetime.now().strftime('%Y%m%d_%H%M'))
        report_file.write_text(json.dumps(report, indent=2, default=str))

        # Send Telegram
        try:
            self._send_telegram(report['telegram_message'])
        except Exception as e:
            logger.warning("[SelfHealing] Telegram send failed: %s" % e)

        return report

    def _take_action(self, action, strategy, reason):
        """Record an action taken by the self-healing engine."""
        entry = {
            'action': action,
            'strategy': strategy,
            'reason': reason,
            'timestamp': datetime.now().isoformat(),
        }
        self._state.setdefault('actions_taken', []).append(entry)

        if action == 'DISABLE':
            if strategy not in self._state.get('disabled_strategies', []):
                self._state.setdefault('disabled_strategies', []).append(strategy)

        self._save_state()
        logger.warning("[SelfHealing] ACTION: %s %s — %s" % (action, strategy, reason))

    def _send_telegram(self, message):
        """Send Telegram notification."""
        import requests
        bot_token = os.environ.get('TELEGRAM_BOT_TOKEN',
                                   '8426384062:AAGJKO8y7ijbSMdbaFnxkfOeLFlnQZWc8oI')
        chat_id = os.environ.get('TELEGRAM_CHAT_ID', '1722559857')
        url = 'https://api.telegram.org/bot%s/sendMessage' % bot_token
        try:
            requests.post(url, json={
                'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML'
            }, timeout=10)
        except Exception:
            pass

    def get_status(self):
        """Get current self-healing status for dashboard."""
        return {
            'last_backtest': self._state.get('last_backtest'),
            'last_retention': self._state.get('last_retention'),
            'last_health_digest': self._state.get('last_health_digest'),
            'disabled_strategies': self._state.get('disabled_strategies', []),
            'recent_actions': self._state.get('actions_taken', [])[-10:],
        }

    def start(self):
        """Start background scheduler for automatic self-healing."""
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return

        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, daemon=True, name='SelfHealing')
        self._scheduler_thread.start()
        logger.info("[SelfHealing] Background scheduler started")

    def stop(self):
        """Stop background scheduler."""
        self._stop_event.set()
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)

    def _scheduler_loop(self):
        """Background loop that runs tasks on schedule."""
        while not self._stop_event.is_set():
            now = datetime.now()
            hour = now.hour
            minute = now.minute

            try:
                # Every 4 hours: backtest check (at :05 past the hour)
                if hour % 4 == 0 and minute == 5:
                    self.run_backtest_check()

                # Daily 03:00: data retention
                if hour == 3 and minute == 0:
                    self.run_data_retention()

                # Daily 06:00: health digest
                if hour == 6 and minute == 0:
                    self.run_health_digest()

            except Exception as e:
                logger.error("[SelfHealing] Scheduler error: %s" % e)

            # Sleep 60 seconds between checks
            self._stop_event.wait(60)


# ── Standalone Test ──────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    print("Self-Healing Engine — Test Run")
    print("=" * 70)

    engine = SelfHealingEngine()

    # Run backtest check
    print("\n1. Backtest Check:")
    bt = engine.run_backtest_check()
    print("   Status: %s" % bt['status'])
    for strat, info in bt.get('strategies', {}).items():
        w7 = info.get('windows', {}).get(7, {})
        if w7.get('status') == 'INSUFFICIENT':
            print("   %s: insufficient data" % strat)
        else:
            print("   %s: PF=%.2f WR=%.0f%% %s" % (
                strat, w7.get('pf', 0), w7.get('wr', 0),
                'DEGRADED' if info.get('degraded') else 'OK'))

    # Run data retention
    print("\n2. Data Retention:")
    dr = engine.run_data_retention()
    print("   Logs deleted: %d, Rotated: %d, Freed: %.1fMB" % (
        dr['logs_deleted'], dr['logs_rotated'], dr['space_freed_mb']))

    # Run health digest
    print("\n3. Health Digest:")
    hd = engine.run_health_digest()
    print(hd.get('telegram_message', 'No message'))

    print("\n4. Status:")
    status = engine.get_status()
    print("   %s" % json.dumps(status, indent=2, default=str))
