@echo off
REM ==========================================================================
REM  LINGLING START-ALL  --  one command to rule them all
REM
REM  Double-click this file (or run it in a terminal). It will:
REM    1. Start the Lingling backend on http://localhost:8000
REM    2. Bootstrap N Tor egress lanes pinned to distinct exit countries via
REM       tor.exe, so the pool carries genuine distinct exit IPs and your
REM       rate-limited IP is bypassed automatically.
REM
REM  Step 2 runs *inside* the server via an env flag (no auth needed -- the
REM  gateway is open, just point your editor at the base URL). The flag:
REM    LINGLING_BOOTSTRAP_TOR=1             download tor.exe + bring up Tor lanes
REM
REM  Then open http://localhost:8000 in your browser for the dashboard, or use
REM  the OpenAI-compatible endpoint at http://localhost:8000/v1/chat/completions
REM
REM  To stop: close the "Lingling Backend" window. The tor.exe processes are
REM  bound to the server by a kill-on-close job, so they die with it -- no
REM  manual taskkill needed.
REM ==========================================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo   Lingling one-click starter
echo ============================================================
echo.

echo [1/2] Starting Lingling backend on http://localhost:8000 ...
start "Lingling Backend" /MIN cmd /c "cd /d %~dp0 && set LINGLING_BOOTSTRAP_TOR=1 && python app.py"

REM Wait for the backend to report healthy. /api/health is deliberately keyless,
REM so a liveness check like this one needs no credential.
echo [1/2] Waiting for Lingling backend to be ready ...
set "retries=30"
:wait_loop
curl -s -f -o nul http://127.0.0.1:8000/api/health
if %errorlevel% == 0 goto backend_ready
timeout /t 1 /nobreak >nul
set /a retries-=1
if %retries% gtr 0 goto wait_loop
echo Backend did not start in time. Check the "Lingling Backend" window.
exit /b 1
:backend_ready
echo Backend ready.

REM Tor bootstrap runs on a background thread inside the server, so the pool
REM fills in shortly after this point. The first run downloads the Tor expert
REM bundle and builds one circuit per exit country (default us, de, nl, fr, ro).
REM It takes a minute or two; later runs reuse everything and are near-instant.
REM Watch the backend window, or the Egress view on the dashboard.
echo [2/2] Tor lanes filling in the background (first run: 1-2 min) ...

echo.
echo ============================================================
echo   DONE. Lingling is live.
echo   Dashboard : http://localhost:8000
echo   Chat API  : http://localhost:8000/v1/chat/completions
echo   Egress    : http://localhost:8000/#egress
echo ============================================================
echo.
echo   No API key needed - the gateway is open. Point your editor at
echo   http://localhost:8000/v1 and go.
echo.
echo   To STOP everything later:
echo     close the "Lingling Backend" window - the Tor lanes stop with it
echo     (kill-on-close job).
echo.
pause
