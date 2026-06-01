@echo off
REM ============================================================
REM  Re-import your coverage list from the Excel master into
REM  watchlist.csv (which the dashboard reads).
REM
REM  Use this when you've edited the source workbook:
REM    C:\Users\jeffl\OneDrive\文档\Aqua Lake Capital - Interview\JL coverage_v3.xlsx
REM
REM  After it finishes, refresh the dashboard (or restart it).
REM ============================================================
cd /d "%~dp0"
python build_watchlist.py
echo.
echo Done. Refresh the dashboard tab (F5) to see the changes.
pause
