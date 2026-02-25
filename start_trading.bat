@echo off
echo =============================================
echo   ALGO TRADING - AUTO START
echo   %date% %time%
echo =============================================
echo.

cd /d C:\Users\Ram\Data\algo_trading

echo Starting Paper Trader (continuous mode)...
start "Paper_Trader" cmd /k "C:\Users\Ram\AppData\Local\Programs\Python\Python312\python.exe" run_paper_trade.py

echo Starting Live Scanner (1-min intervals)...
start "Live_Scanner" cmd /k "C:\Users\Ram\AppData\Local\Programs\Python\Python312\python.exe" live_scanner.py

echo.
echo Both systems launched in separate windows.
echo   Paper Trader: Equity 9:15-15:30, Commodity 15:30-23:30
echo   Live Scanner: OI/Greeks/IV capture every minute
echo.
pause
