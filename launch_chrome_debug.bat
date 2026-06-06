@echo off
echo.
echo ============================================================
echo  Job Agent - Chrome Debug Launcher
echo ============================================================
echo.
echo Opens a SEPARATE Chrome window for job applications.
echo Your normal Chrome keeps running unaffected.
echo.
echo First time only: log into Indeed in this window.
echo After that: sessions are saved here automatically.
echo.

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"

REM Dedicated profile dir — completely separate from your normal Chrome
set JOBAGENT_DIR=%LOCALAPPDATA%\Google\Chrome\JobAgent

if not exist %CHROME% (
    echo ERROR: Chrome not found at %CHROME%
    echo Please update this script with the correct Chrome path.
    pause
    exit /b 1
)

REM Create the JobAgent dir if this is the first run
if not exist "%JOBAGENT_DIR%" (
    mkdir "%JOBAGENT_DIR%"
    echo Created job agent profile at %JOBAGENT_DIR%
)

echo Starting Job Agent Chrome on debug port 9222...
start "" %CHROME% ^
    --remote-debugging-port=9222 ^
    --user-data-dir="%JOBAGENT_DIR%" ^
    --profile-directory=Default ^
    --no-first-run ^
    --no-default-browser-check ^
    --window-size=1280,900 ^
    --window-position=0,0

echo.
echo Job Agent Chrome started (port 9222).
echo First time: log into Indeed in that window, then run the overnight runner.
echo Later runs: Indeed login is already saved, just run the overnight runner.
echo.
