@echo off
REM Double-click this file to launch the Asia Watchlist Monitor.
REM It starts the dashboard and opens it in your browser.
cd /d "%~dp0"
echo Starting Asia Watchlist Monitor...
echo A browser tab will open at http://localhost:8501
echo Keep this window open while using the dashboard. Close it to stop.
python -m streamlit run monitor.py --server.port 8501
pause
