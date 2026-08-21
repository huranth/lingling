@echo off
REM ==========================================================================
REM  LINGLING START-ALL  --  one command to rule them all
REM
REM  Double-click this file (or run it in a terminal). It will:
REM    1. Start the Lingling backend on http://localhost:8000
REM    2. Register WARP identities (one-time) and start all wireproxy SOCKS5
REM       proxies, so your rate-limited IP is bypassed automatically.
REM
REM  Step 2 runs *inside* the server via LINGLING_BOOTSTRAP_WARP=1. It used to be
REM  two curl POSTs to /api/warp/setup and /api/warp/start, but every /api/ route
REM  is authenticated now -- this launcher would have needed an API key to
REM  bootstrap the server it had just started. In-process, no credential needed.
REM
REM  Then open http://localhost:8000 in your browser for the dashboard, or use
REM  the OpenAI-compatible endpoint at http://localhost:8000/v1/chat/completions
REM
REM  To stop: close the "Lingling Backend" window. The WARP wireproxy
REM  processes are bound to the server by a kill-on-close job, so they die
REM  with it -- no manual taskkill needed.
REM ==========================================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo   Lingling + WARP -- one click, and we're off.
echo ============================================================
echo.

echo [1/2] Waking Lingling up on http://localhost:8000 ...
REM Use the `py` launcher, not `python`: PATH's `python` resolves to Python 3.12
REM here, which has no fastapi, so the backend would crash on import. `py -3`
REM runs the default 3.x (3.14 on this machine), where the requirements live.
REM
REM Lingling logs itself to data\backend.log from inside the app (a FileHandler
REM wired in app._build_log_config), so this launcher does NOT redirect stdout.
REM The minimized window shows the live (minimal, WARP-verbose-off) output
REM instead of a blank pane. It stays open on a crash (cmd /k, not /c), so even
REM an import-time stack trace -- before the file handler attaches -- is still
REM readable there; runtime traces also land in data\backend.log. Clear the
REM file by deleting it.
if not exist "%~dp0data" mkdir "%~dp0data"
start "Lingling Backend" /MIN cmd /k "cd /d %~dp0 && set LINGLING_BOOTSTRAP_WARP=1 && py -3 app.py"

REM Wait for the backend to report healthy. /api/health is deliberately keyless,
REM so a liveness check like this one needs no credential.
echo [1/2] Poking Lingling until it answers on :8000 ...
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
echo Backend ready. Lingling's awake and mildly annoyed.

REM WARP registration runs on a background thread inside the server, so the pool
REM fills in shortly after this point. The first run downloads wgcf + wireproxy
REM and registers 10 identities, which takes a minute or two; later runs reuse
REM them and are near-instant. Watch data\backend.log (or the Egress view).
echo [2/2] WARP pool registering in the background (first run: 1-2 min) -- opencode won't know what hit it.

echo.
echo ============================================================
echo   DONE. Lingling is live and taking requests.
echo   Dashboard : http://localhost:8000
echo   Chat API  : http://localhost:8000/v1/chat/completions
echo   Egress    : http://localhost:8000/#egress
echo   Backend log: data\backend.log (crash traces kept here)
echo ============================================================
echo.
echo   API clients (Cline, Claude Code, Jan) need a key:
echo     open the dashboard, go to Keys, create one -- yes, even you.
echo   The dashboard itself uses a session cookie -- no key required.
echo.
echo   To STOP everything later:
echo     close the "Lingling Backend" window - WARP proxies stop with it
echo.
pause
