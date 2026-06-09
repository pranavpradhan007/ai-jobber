"""
Launch the Playwright Chromium browser with CDP port 9222 and the saved
browser_profile (which has LinkedIn + Indeed sessions).

Run this ONCE before running discovery or fill_only.
Chrome stays open in the background; Ctrl+C kills it.
"""
import os, sys, pathlib, subprocess, time, requests

CHROME = r"C:\Users\hp\AppData\Local\ms-playwright\chromium-1181\chrome-win\chrome.exe"
PROFILE_DIR = str(pathlib.Path("browser_profile").resolve())
PORT = 9222
LANDING = "https://www.linkedin.com/jobs/"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _cdp_ready(port: int) -> bool:
    try:
        r = requests.get(f"http://localhost:{port}/json/version", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


if _cdp_ready(PORT):
    print(f"Chrome CDP already running on port {PORT}.")
    sys.exit(0)

# Kill any stale chrome.exe first
subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], capture_output=True)
time.sleep(2)

pathlib.Path(PROFILE_DIR).mkdir(parents=True, exist_ok=True)

proc = subprocess.Popen(
    [
        CHROME,
        f"--remote-debugging-port={PORT}",
        f"--user-data-dir={PROFILE_DIR}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,900",
        "--disable-blink-features=AutomationControlled",
        LANDING,
    ],
    close_fds=True,
)

print(f"Launching Chrome (pid={proc.pid}) on port {PORT}...")
for i in range(25):
    time.sleep(1)
    if _cdp_ready(PORT):
        print(f"  Ready after {i+1}s — Chrome is open at {LANDING}")
        break
else:
    print("  CDP did not start in 25s — check if Chrome is already running")
    sys.exit(1)

print("Chrome is running. Press Ctrl+C to close.")
try:
    proc.wait()
except KeyboardInterrupt:
    proc.terminate()
    print("Chrome closed.")
