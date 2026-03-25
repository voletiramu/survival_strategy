#!/usr/bin/env python3
"""Add get_intraday_ohlc stub to StockPaperTrader."""

f = "/root/algo_trading/stock_paper_trader.py"
with open(f) as fh:
    lines = fh.readlines()

if any("def get_intraday_ohlc" in l for l in lines):
    print("get_intraday_ohlc already exists")
else:
    # Find "    def run_once(self):" and insert before it
    for i, line in enumerate(lines):
        if line.strip() == "def run_once(self):" and lines[i].startswith("    "):
            stub_lines = [
                "\n",
                "    def get_intraday_ohlc(self, symbol):\n",
                '        """v17: Stub for signal logging compatibility."""\n',
                "        try:\n",
                '            if hasattr(self, "_last_ohlc") and symbol in self._last_ohlc:\n',
                "                return self._last_ohlc[symbol]\n",
                "        except Exception:\n",
                "            pass\n",
                "        return None\n",
                "\n",
            ]
            for j, sl in enumerate(stub_lines):
                lines.insert(i + j, sl)
            print("Added get_intraday_ohlc stub before run_once")
            break

    with open(f, "w") as fh:
        fh.writelines(lines)

import py_compile
py_compile.compile(f, doraise=True)
print("Syntax OK")
