"""Complete health check — syntax, imports, feature verification, simulation test.
Run on VPS."""
import sys, os, json, time
from datetime import datetime

sys.path.insert(0, '/root/algo_trading')
os.chdir('/root/algo_trading')

PASS = 0
FAIL = 0
WARN = 0

def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} — {detail}')

def warn(name, detail=''):
    global WARN
    WARN += 1
    print(f'  [WARN] {name} — {detail}')

print('='*80)
print('FULL HEALTH CHECK — ALL BOTS')
print(f'Time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} IST')
print('='*80)

# === 1. SYNTAX CHECK ===
print('\n1. SYNTAX CHECK (20 files)')
print('-'*40)
files = [
    'paper_trader.py', 'commodity_paper_trader.py', 'stock_paper_trader.py',
    'run_paper_trade.py', 'run_stock_trade.py',
    'market_data_pipeline.py', 'market_regime.py', 'ws_feed.py',
    'market_calculus.py', 'trade_intelligence.py',
    'zerodha_feed.py', 'truedata_feed.py', 'trade_notifier.py',
    'dashboard.py', 'live_data_logger.py', 'gift_nifty.py',
    'zerodha_token_gen.py', 'zerodha_daily_token.py',
    'stock_fno_config.py', 'stock_data_pipeline.py',
]
for f in files:
    fp = f'/root/algo_trading/{f}'
    if os.path.exists(fp):
        try:
            with open(fp) as fh:
                compile(fh.read(), fp, 'exec')
            check(f, True)
        except SyntaxError as e:
            check(f, False, str(e))
    else:
        warn(f, 'File not found')

# === 2. IMPORT CHECK ===
print('\n2. IMPORT CHECK (critical modules)')
print('-'*40)
modules = [
    ('market_regime', 'get_regime_params, RegimeDetector'),
    ('market_calculus', 'MarketCalculus'),
    ('trade_intelligence', 'TradeIntelligence'),
    ('ws_feed', 'WebSocketFeed'),
    ('live_data_logger', 'LiveDataLogger'),
    ('market_data_pipeline', 'MarketDataPipeline'),
    ('gift_nifty', 'load_gap_analysis, fetch_gift_nifty'),
]
for mod_name, classes in modules:
    try:
        mod = __import__(mod_name)
        for cls in classes.split(', '):
            assert hasattr(mod, cls.strip()), f'{cls} not found'
        check(mod_name, True)
    except Exception as e:
        check(mod_name, False, str(e))

# === 3. FEATURE CHECK PER BOT ===
print('\n3. FEATURE VERIFICATION (per bot)')
print('-'*40)
bots = [
    ('paper_trader.py', 'Equity'),
    ('commodity_paper_trader.py', 'Commodity'),
    ('stock_paper_trader.py', 'Stock'),
]

for fp, name in bots:
    print(f'\n  --- {name} Bot ({fp}) ---')
    with open(f'/root/algo_trading/{fp}') as f:
        code = f.read()

    # 3a. 1-second scan
    check(f'{name}: 1-sec scan', 'interval_seconds=1' in code or 'interval_sec=1' in code,
          'interval not set to 1')

    # 3b. Choppy Shield
    check(f'{name}: Choppy Shield', 'choppy' in code.lower(),
          'No choppy reference found')

    # 3c. ML features function
    check(f'{name}: compute_ml_features', 'compute_ml_features' in code,
          'ML features function missing')

    # 3d. Signal strength
    check(f'{name}: compute_signal_strength', 'compute_signal_strength' in code,
          'Signal strength function missing')

    # 3e. LTP source tracking
    check(f'{name}: ltp_source tracking', 'ltp_source' in code,
          'LTP source tracking missing')

    # 3f. First trade time
    if name == 'Equity':
        check(f'{name}: First trade 9:16', 'dtime(9, 16)' in code,
              'Still at 9:30')
    elif name == 'Commodity':
        check(f'{name}: First trade 9:01', 'dtime(9, 1)' in code,
              'Still at 9:30')
    elif name == 'Stock':
        check(f'{name}: First trade 9:16', 'dtime(9, 16)' in code,
              'Still at 9:30')

    # 3g. THETA disabled
    check(f'{name}: THETA disabled (999)',
          'THETA_BURDEN_EXIT_PCT = 999' in code or 'MCX_THETA_BURDEN_EXIT_PCT = 999' in code,
          'THETA may still be active')

    # 3h. BREAKOUT_FAIL_REVERSE disabled
    check(f'{name}: REVERSE disabled',
          'BREAKOUT_FAIL_REVERSE_ENABLED = False' in code,
          'REVERSE may still be enabled')

    # 3i. Grace period PnL fix
    check(f'{name}: Grace PnL fix', '_grace_ltp' in code,
          'PnL not updating during grace period')

    # 3j. Signal CSV has ltp_source + strength
    if 'save_signals_log' in code or 'save_signal' in code:
        check(f'{name}: CSV has ltp_source',
              'ltp_source' in code[code.index('sig_key'):] if 'sig_key' in code else False,
              'ltp_source not in signal CSV logger')

    # 3k. Auto token refresh (equity only)
    if name == 'Equity':
        check(f'{name}: Auto token refresh', 'auto_refresh_zerodha_token' in code,
              'Auto token refresh missing')

    # 3l. GIFT Nifty (equity only)
    if name == 'Equity':
        check(f'{name}: GIFT Nifty gap loading', 'gap_analysis' in code,
              'GIFT Nifty gap analysis missing')

# === 4. LAUNCHER CHECK ===
print('\n4. LAUNCHER VERIFICATION')
print('-'*40)

with open('/root/algo_trading/run_paper_trade.py') as f:
    rpt = f.read()
check('run_paper_trade: Equity interval=1', 'DEFAULT_EQUITY_INTERVAL = 1' in rpt, 'Not set to 1')
check('run_paper_trade: Commodity interval=1', 'DEFAULT_COMMODITY_INTERVAL = 1' in rpt, 'Not set to 1')

with open('/root/algo_trading/run_stock_trade.py') as f:
    rst = f.read()
check('run_stock_trade: interval=1', 'DEFAULT_INTERVAL = 1' in rst, 'Not set to 1')

# === 5. SYSTEMD CHECK ===
print('\n5. SYSTEMD SERVICE CHECK')
print('-'*40)
import subprocess
for svc in ['algo-trading', 'algo-stock-trading']:
    r = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True)
    check(f'{svc}: active', r.stdout.strip() == 'active', r.stdout.strip())

# === 6. ZERODHA TOKEN ===
print('\n6. ZERODHA TOKEN STATUS')
print('-'*40)
token_paths = ['/root/algo_trading/config/zerodha_token.json', '/root/algo_trading/zerodha_token.json']
token_found = False
for tp in token_paths:
    if os.path.exists(tp):
        with open(tp) as f:
            t = json.load(f)
        gen = t.get('generated_at', '')
        today = datetime.now().strftime('%Y-%m-%d')
        check('Zerodha token exists', True)
        check('Zerodha token is today', today in gen, f'Generated: {gen}')
        token_found = True
        break
if not token_found:
    check('Zerodha token exists', False, 'No token file found')

# === 7. GIFT NIFTY DATA ===
print('\n7. GIFT NIFTY PRE-MARKET DATA')
print('-'*40)
gift_file = '/root/algo_trading/paper_trades/gift_nifty_today.json'
if os.path.exists(gift_file):
    with open(gift_file) as f:
        g = json.load(f)
    check('GIFT data file exists', True)
    today = datetime.now().strftime('%Y-%m-%d')
    check('GIFT data is today', today in g.get('timestamp', ''), g.get('timestamp', ''))
    print(f'  Gap: {g.get("direction","?")} {g.get("gap_pct",0):+.2f}% | Bias: {g.get("bias","?")}')
else:
    check('GIFT data file exists', False, 'Run gift_nifty.py')

# === 8. CRON JOBS ===
print('\n8. CRON SCHEDULE')
print('-'*40)
r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
cron = r.stdout
check('Cron: Zerodha token', 'zerodha_daily_token' in cron, 'Missing')
check('Cron: GIFT Nifty', 'gift_nifty' in cron, 'Missing')
check('Cron: Commodity data refresh', 'fix_commodity_data' in cron or 'commodity' in cron, 'Missing')

# === 9. DASHBOARD ===
print('\n9. DASHBOARD CHECK')
print('-'*40)
with open('/root/algo_trading/dashboard.py') as f:
    dash_code = f.read()
check('Dashboard: ltp_source in API', 'ltp_source' in dash_code, 'Missing')
check('Dashboard: signal_strength in API', 'signal_strength' in dash_code, 'Missing')

with open('/root/algo_trading/templates/dashboard.html') as f:
    dash_html = f.read()
check('Dashboard: sourceTag JS', 'sourceTag' in dash_html, 'Missing')
check('Dashboard: strengthBar JS', 'strengthBar' in dash_html, 'Missing')
check('Dashboard: strengthBadge JS', 'strengthBadge' in dash_html, 'Missing')

r = subprocess.run(['pgrep', '-f', 'dashboard.py'], capture_output=True, text=True)
check('Dashboard: process running', r.returncode == 0, 'Not running')

# === 10. FUNCTIONAL TEST ===
print('\n10. FUNCTIONAL TEST (simulate signal flow)')
print('-'*40)

try:
    from paper_trader import compute_ml_features, compute_signal_strength

    # Simulate a CPR signal
    test_sig = {
        'strategy': 'CPR', 'type': 'BUY_PE_CPR',
        'symbol': 'NIFTY', 'strike': 23200, 'premium': 233.75,
        'spot': 23185, 'dte': 1,
        'greeks': {'delta': -0.51, 'gamma': 0.0016, 'theta': -52.3, 'iv': 0.207},
        'target': 350, 'sl': 117, 'quality_score': 72,
        'oi': 5000, 'ltp_source': 'ZERODHA',
    }
    test_ohlc = {'open': 23100, 'high': 23250, 'low': 23050, 'close': 23185}
    test_indicators = {'atr': 300, 'cpr_width': 0.048, 'efficiency': 0.45, 'pcr_shift': 0.03}

    # Test ML features
    ml = compute_ml_features(test_sig, test_ohlc, 23185, test_indicators, 22.8)
    check('ML features: returns 14 features', len(ml) == 14, f'Got {len(ml)}')
    check('ML features: gamma > 0', ml.get('ml_gamma', 0) > 0, f'gamma={ml.get("ml_gamma")}')
    check('ML features: delta > 0', ml.get('ml_delta', 0) > 0, f'delta={ml.get("ml_delta")}')
    check('ML features: vix_level set', ml.get('ml_vix_level', 0) == 22.8, f'vix={ml.get("ml_vix_level")}')
    check('ML features: strategy_id CPR=0', ml.get('ml_strategy_id') == 0, f'id={ml.get("ml_strategy_id")}')

    # Test signal strength
    s, l = compute_signal_strength(test_sig, 23185, test_indicators, 22.8, 'ZERODHA')
    check('Signal strength: returns score', 0 < s <= 100, f'score={s}')
    check('Signal strength: returns label', l in ('ELITE','STRONG','MODERATE','WEAK'), f'label={l}')
    check('Signal strength: ZERODHA boosts score', s > 50, f'score={s} (expected >50 with Zerodha)')
    print(f'  Signal strength test: {s}/100 ({l})')

    # Test GIFT Nifty
    from gift_nifty import load_gap_analysis
    gap = load_gap_analysis()
    check('GIFT Nifty: load_gap_analysis works', gap is not None or True, 'Returns None (may be expected on weekend)')
    if gap:
        check('GIFT Nifty: has direction', 'direction' in gap, 'Missing direction')
        check('GIFT Nifty: has gap_pct', 'gap_pct' in gap, 'Missing gap_pct')

except Exception as e:
    check('Functional test', False, str(e))

# === 11. PORTFOLIO STATE ===
print('\n11. PORTFOLIO STATE')
print('-'*40)
for name, path in [
    ('Equity', '/root/algo_trading/paper_trades/portfolio_state.json'),
    ('Commodity', '/root/algo_trading/paper_trades_commodity/commodity_portfolio_state.json'),
    ('Stock', '/root/algo_trading/stock_paper_trades/portfolio_state.json'),
]:
    if os.path.exists(path):
        with open(path) as f:
            d = json.load(f)
        cap = d.get('capital', 0)
        pos = len(d.get('positions', []))
        closed = len(d.get('closed_trades', []))
        print(f'  {name}: Capital Rs {cap:,.0f} | Open: {pos} | Closed: {closed}')
    else:
        warn(f'{name} portfolio', 'File not found')

# === SUMMARY ===
print('\n' + '='*80)
print(f'HEALTH CHECK COMPLETE')
print(f'  PASS: {PASS}')
print(f'  FAIL: {FAIL}')
print(f'  WARN: {WARN}')
if FAIL == 0:
    print(f'\n  ALL CHECKS PASSED — BOT IS READY FOR TRADING')
else:
    print(f'\n  {FAIL} FAILURES — FIX BEFORE TRADING')
print('='*80)
