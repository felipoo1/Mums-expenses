"""Open each Streamlit app in a real (headless) browser so it counts as a visit.
If the app is asleep, press the wake-up button. Plain web requests do not work:
the app only wakes when a browser actually loads it."""
import os
import sys
from playwright.sync_api import sync_playwright

urls = [u.strip() for u in os.environ.get("APP_URLS", "").replace("\n", ",").split(",") if u.strip()]
if not urls:
    sys.exit("Add APP_URLS in Settings > Secrets and variables > Actions > Variables")

failed = False
with sync_playwright() as p:
    browser = p.chromium.launch()
    for url in urls:
        page = browser.new_page()
        try:
            page.goto(url, timeout=90000)
            page.wait_for_timeout(8000)
            wake = page.get_by_role("button", name="Yes, get this app back up!")
            if wake.count():
                wake.first.click()
                page.wait_for_timeout(60000)
                print("WOKE UP ", url)
            else:
                print("AWAKE   ", url)
        except Exception as e:  # noqa: BLE001
            failed = True
            print("FAILED  ", url, type(e).__name__, e)
        finally:
            page.close()
    browser.close()
sys.exit(1 if failed else 0)
