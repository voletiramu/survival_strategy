#!/usr/bin/env python3
"""Add greeks cache to reduce Angel API rate limit hits."""

for filepath in ['/root/algo_trading/commodity_paper_trader.py']:
    with open(filepath, 'r') as f:
        code = f.read()

    # Add a cache to get_option_greeks to avoid hammering Angel
    old = '''    def get_option_greeks(self, commodity, expiry=None):
        """Get option greeks including IV from Angel API."""
        if not self._connected:
            return None
        try:
            self._throttle()'''

    new = '''    def get_option_greeks(self, commodity, expiry=None):
        """Get option greeks including IV from Angel API."""
        if not self._connected:
            return None
        # v13.7: Cache greeks for 30s to avoid Angel rate limits (196 errors/day)
        import time as _time
        _cache_key = f"{commodity}_{expiry}"
        if not hasattr(self, '_greeks_cache'):
            self._greeks_cache = {}
            self._greeks_cache_time = {}
        if _cache_key in self._greeks_cache:
            _age = _time.time() - self._greeks_cache_time.get(_cache_key, 0)
            if _age < 30:
                return self._greeks_cache[_cache_key]
        try:
            self._throttle()'''

    if old in code:
        code = code.replace(old, new)

        # Also cache the result before return
        old_return = '''            if data and data.get('data'):
                return data['data']
        except Exception as e:
            logger.error(f"MCX option greeks error: {e}")
        return None'''

        new_return = '''            if data and data.get('data'):
                self._greeks_cache[_cache_key] = data['data']
                self._greeks_cache_time[_cache_key] = _time.time()
                return data['data']
        except Exception as e:
            logger.error(f"MCX option greeks error: {e}")
        return self._greeks_cache.get(_cache_key)'''

        code = code.replace(old_return, new_return)

        import ast
        ast.parse(code)
        with open(filepath, 'w') as f:
            f.write(code)
        print(f"Added 30s greeks cache to {filepath}")
    else:
        print(f"Pattern not found in {filepath}")
