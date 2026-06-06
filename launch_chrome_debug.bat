@echo off
echo.
echo ============================================================
echo  Job Agent - Chrome Debug Launcher
echo ============================================================
echo.
echo This opens a real Chrome window with remote debugging enabled.
echo After Chrome opens:
echo   1. Log into Indeed (indeed.com) if not already logged in
echo   2. Keep this Chrome window open
echo   3. Run: python run_live_overnight.py
echo.
echo Chrome will use your Default profile (all your logins intact).
echo.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set USERDATA=%LOCALAPPDATA%\Google\Chrome\User Data

if not exist %CHROME% (
    echo ERROR: Chrome not found at %CHROME%
    echo Please update this script with the correct Chrome path.
    pause
    exit /b 1
)

echo Starting Chrome on debug port 9222...
start "" %CHROME% --remote-debugging-port=9222 --user-data-dir=%USERDATA% --profile-directory=Default --no-first-run --no-default-browser-check

echo.
echo Chrome started. Log into Indeed, then run the overnight runner.
echo.
