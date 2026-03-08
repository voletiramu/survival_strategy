@echo off
REM Run OI Snapshot Collector daily at 3:25 PM IST
REM Schedule via Windows Task Scheduler:
REM   1. Open Task Scheduler > Create Basic Task
REM   2. Trigger: Daily at 3:25 PM
REM   3. Action: Start a program
REM   4. Program: C:\Users\Ram\Data\algo_trading\run_oi_collector.bat

cd /d C:\Users\Ram\Data\algo_trading
python oi_snapshot_collector.py >> logs\oi_collector.log 2>&1
