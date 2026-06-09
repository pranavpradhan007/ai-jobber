@echo off
:: ============================================================
::  Job Agent — Auto-Start Script
::  Runs on Windows login via Task Scheduler.
::  Opens two terminals:
::    Terminal 1 — Chrome debug mode (LinkedIn/Indeed session)
::    Terminal 2 — Python continuous loop (discovery + submit + digest)
:: ============================================================

set "JOBDIR=E:\job search"
set "CHROME=C:\Users\hp\AppData\Local\ms-playwright\chromium-1181\chrome-win\chrome.exe"
set "USERDATA=C:\Users\hp\AppData\Local\Google\Chrome\JobAgent"

echo.
echo [job-agent] Starting Chrome debug session...
if exist "%CHROME%" (
    start "" "%CHROME%" ^
        --remote-debugging-port=9222 ^
        --user-data-dir="%USERDATA%" ^
        --profile-directory=Default ^
        --no-first-run ^
        --no-default-browser-check ^
        --window-size=1280,900 ^
        --window-position=0,0
) else (
    echo [job-agent] WARNING: Playwright Chrome not found — trying system Chrome
    set "CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe"
    if exist "%CHROME%" (
        start "" "%CHROME%" ^
            --remote-debugging-port=9222 ^
            --user-data-dir="%USERDATA%" ^
            --profile-directory=Default ^
            --no-first-run ^
            --no-default-browser-check ^
            --window-size=1280,900
    )
)

:: Wait for Chrome to open its debug port
timeout /t 4 /nobreak >nul

echo [job-agent] Starting continuous loop in new terminal...
start "Job-Agent Loop" cmd /k "cd /d "%JOBDIR%" && python run_continuous_loop.py"

echo [job-agent] All components started.
