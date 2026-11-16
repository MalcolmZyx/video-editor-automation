"""Open a page in headless Chrome in REAL time and print the page title once it starts with a given prefix.

    python tools/browser_title.py URL PREFIX [timeout_s]

The editor's built-in test hooks (#perf, #robust, ...) report their results in document.title.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

url, prefix = sys.argv[1], sys.argv[2]
limit = float(sys.argv[3]) if len(sys.argv) > 3 else 120
chrome = next((p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") if os.path.exists(p)), "chrome")
port = 9333
proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--window-size=1500,950", f"--remote-debugging-port={port}",
                         f"--user-data-dir={tempfile.mkdtemp()}", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    end = time.time() + limit
    title = ""
    while time.time() < end:
        time.sleep(0.5)
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2))
        except Exception:  # noqa: BLE001, S112 - Chrome is still starting; just try again
            continue
        title = next((t["title"] for t in tabs if t.get("type") == "page" and t["title"].startswith(prefix)), "")
        if title:
            break
    print(title or "TIMEOUT")
finally:
    proc.kill()
