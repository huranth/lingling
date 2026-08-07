@echo off
REM ==========================================================================
REM  LINGLING -- CODEX SETUP (the ONE thing for Codex)
REM
REM  Double-click this. It does everything, in order:
REM    [1/2] Refreshes the Codex catalog from the LIVE model list (new free
REM          models + their effort levels appear automatically) and wires
REM          ~/.codex/config.toml.
REM    [2/2] Opens the API-key window: it auto-fills a key already on the
REM          dashboard (or mints one), and Apply saves it as LINGLING_API_KEY
REM          so you never type "set LINGLING_API_KEY=..." again.
REM
REM  Then open a NEW terminal and run  codex.
REM ==========================================================================
setlocal
cd /d "%~dp0backend"

echo ============================================================
echo   Lingling - one-click Codex setup
echo ============================================================
echo.
echo [1/2] Refreshing the Codex catalog (live models + effort levels)...
py -3 tools\codex_catalog.py --no-key-setup
if errorlevel 1 goto :fail

echo.
echo [2/2] Opening the API-key window...
py -3 -m codex.setup_gui
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   DONE. Open a NEW terminal and run:  codex
echo   The catalog and key are wired - no manual steps.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo Something went wrong - the error is above. Press any key to close.
pause >nul
exit /b 1