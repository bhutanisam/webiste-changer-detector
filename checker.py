"""checker.py - everything about checking a page:
fetch it in a headless browser, clean the noise, compare hashes,
save snapshots, send alerts, and the job queue that runs the checks."""
import hashlib
import ipaddress
import os
import re
import smtplib
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

import db

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")  # used in alert links
RETRIES = 3                 # 1 try + 2 retries
PAGE_TIMEOUT_MS = 30000     # 30 second timeout per page load
DOMAIN_DELAY_SECONDS = 10   # wait at least this long between requests to one site
NOISY_LIMIT = 5             # this many changes in a row = page is "noisy"

# Parts of a page that change all the time and are not "real" content.
ALWAYS_REMOVE = (
    "script, style, noscript, iframe, "
    "[class*='advert'], [id*='advert'], "
    "[class*='banner'], [class*='cookie'], "
    "[class*='carousel'], [class*='slider']"
)

CAPTCHA_WORDS = ["captcha", "verify you are human", "are you a robot", "unusual traffic"]


# ---------------------------------------------------------------------------
# 1. Safety check for the URL
# ---------------------------------------------------------------------------
def is_public_url(url):
    """Only allow public websites (not localhost or private network addresses)."""
    host = urlparse(url).hostname
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
    except Exception:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local)


# ---------------------------------------------------------------------------
# 2. Be polite: do not hammer one website
# ---------------------------------------------------------------------------
_last_request = {}
_domain_lock = threading.Lock()


def wait_for_domain(url):
    """Make sure requests to the same website are at least 10 seconds apart."""
    domain = urlparse(url).netloc
    with _domain_lock:
        wait = _last_request.get(domain, 0) + DOMAIN_DELAY_SECONDS - time.time()
        _last_request[domain] = time.time() + max(wait, 0)  # reserve our slot
    if wait > 0:
        time.sleep(wait)


# ---------------------------------------------------------------------------
# 3. Fetch the page with a headless browser (Playwright)
# ---------------------------------------------------------------------------
def fetch_page(url, watch_selector, ignore_selector):
    """Returns (http_status, full_page_text, text_to_compare, screenshot_png_bytes)."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="load")
            status_code = response.status if response else 0
            page.wait_for_timeout(1500)  # give JavaScript a moment to finish

            screenshot = page.screenshot()
            full_text = page.inner_text("body")  # used to spot CAPTCHAs

            # Delete ads, banners, scripts and the user's "ignore" areas
            to_remove = ALWAYS_REMOVE
            if ignore_selector.strip():
                to_remove += ", " + ignore_selector
            page.evaluate(
                "(sel) => document.querySelectorAll(sel).forEach(e => e.remove())",
                to_remove,
            )

            # Only look at the "watch" area if the user chose one
            if watch_selector.strip():
                text = page.locator(watch_selector).first.inner_text(timeout=5000)
            else:
                text = page.inner_text("body")
            return status_code, full_text, text, screenshot
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# 4. Noise filtering
# ---------------------------------------------------------------------------
MONTH_DATE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2}(, \d{4})?\b", re.I)
NUMERIC_DATE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\s?(am|pm)?", re.I)
RANDOM_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{16,}\b")  # long strings with digits
NUMBER = re.compile(r"\d+")


def clean_text(text, sensitivity):
    """Remove things that change on every visit, so they do not look like changes.

    high   = keep everything (any text change counts)
    medium = also ignore dates, times and random tokens
    low    = also ignore ALL numbers (view counters, prices, etc.)
    """
    if sensitivity in ("medium", "low"):
        text = MONTH_DATE.sub("", text)
        text = NUMERIC_DATE.sub("", text)
        text = TIME.sub("", text)
        text = RANDOM_TOKEN.sub("", text)
    if sensitivity == "low":
        text = NUMBER.sub("", text)

    # tidy up: collapse spaces, drop empty lines
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def looks_like_captcha(text):
    text = text.lower()
    return any(word in text for word in CAPTCHA_WORDS)


# ---------------------------------------------------------------------------
# 5. Snapshots
# ---------------------------------------------------------------------------
def screenshot_dir():
    return os.path.join(db.DATA_DIR, "screenshots")


def save_snapshot(page_id, text, text_hash, screenshot):
    os.makedirs(screenshot_dir(), exist_ok=True)
    filename = f"{page_id}_{int(time.time())}.png"
    with open(os.path.join(screenshot_dir(), filename), "wb") as f:
        f.write(screenshot)
    return db.execute(
        "INSERT INTO snapshots (page_id, text, text_hash, screenshot, created_at) VALUES (?,?,?,?,?)",
        (page_id, text, text_hash, "screenshots/" + filename, db.now()),
    )


def latest_snapshot(page_id):
    return db.query(
        "SELECT * FROM snapshots WHERE page_id=? ORDER BY id DESC LIMIT 1", (page_id,), one=True
    )


# ---------------------------------------------------------------------------
# 6. Alerts (Discord webhook and email)
# ---------------------------------------------------------------------------
def describe_change(old_text, new_text, old_id, new_id):
    """A short human summary of what changed, plus a link to the full diff."""
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    added = [line for line in new_lines if line not in old_lines]
    removed = [line for line in old_lines if line not in new_lines]

    summary = f"{len(added)} line(s) added, {len(removed)} line(s) removed."
    if added:
        summary += f"\n+ {added[0][:120]}"
    if removed:
        summary += f"\n- {removed[0][:120]}"
    return summary + f"\nSee the diff: {BASE_URL}/compare/{old_id}/{new_id}"


def send_email(to_address, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("Email is not set up (SMTP_HOST missing), skipping email.")
        return
    user = os.environ.get("SMTP_USER", "")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to_address, subject
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=15) as server:
        server.starttls()
        server.login(user, os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)


def send_alert(page, summary):
    """Tell everyone watching this page."""
    title = f"Change detected: {page['name']}"
    message = f"**{title}**\n{page['url']}\n{summary}"
    subscribers = db.query("SELECT * FROM subscribers WHERE page_id=?", (page["id"],))
    for sub in subscribers:
        try:
            if sub["discord_webhook"]:
                requests.post(sub["discord_webhook"], json={"content": message[:1900]}, timeout=10)
            if sub["email"]:
                send_email(sub["email"], title, f"{page['url']}\n{summary}")
        except Exception as error:
            print("Could not send alert:", error)  # one bad webhook must not stop the rest


# ---------------------------------------------------------------------------
# 7. The main check for one page
# ---------------------------------------------------------------------------
def check_page(page_id):
    page = db.query("SELECT * FROM pages WHERE id=?", (page_id,), one=True)
    if page is None:
        return

    status, error, text, screenshot = "error", "", None, None

    # Try up to 3 times (timeouts and network errors are often temporary)
    for attempt in range(RETRIES):
        try:
            wait_for_domain(page["url"])
            code, full_text, found_text, screenshot = fetch_page(
                page["url"], page["watch_selector"], page["ignore_selector"]
            )
        except Exception as e:
            status, error = "error", (str(e).splitlines() or ["unknown error"])[0][:200]
            time.sleep(2)
            continue

        if code in (403, 429) or looks_like_captcha(full_text):
            # Do not retry a block, that only makes it worse. And it is NOT a content change.
            status, error = "blocked", f"Site blocked us (HTTP {code}) or showed a CAPTCHA"
            break
        if code >= 400:
            status, error = "error", f"HTTP error {code}"
            time.sleep(2)
            continue

        status, error, text = "ok", "", found_text
        break

    now = db.now()

    # Outage / block: record it, but do not touch snapshots or send change alerts
    if text is None:
        db.update("pages", page_id, status=status, last_error=error, last_checked=now)
        return

    cleaned = clean_text(text, page["sensitivity"])
    new_hash = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()
    last = latest_snapshot(page_id)

    # First ever check: just save the starting version
    if last is None:
        save_snapshot(page_id, cleaned, new_hash, screenshot)
        db.update("pages", page_id, status="ok", last_error="", last_checked=now, last_changed=now)
        return

    # Same hash = nothing changed, so we store nothing new
    if last["text_hash"] == new_hash:
        db.update("pages", page_id, status="ok", last_error="", last_checked=now, changes_in_a_row=0)
        return

    # Real change: store a new snapshot
    new_id = save_snapshot(page_id, cleaned, new_hash, screenshot)
    streak = page["changes_in_a_row"] + 1
    new_status = "noisy" if streak >= NOISY_LIMIT else "ok"
    db.update(
        "pages", page_id,
        status=new_status, last_error="", last_checked=now, last_changed=now, changes_in_a_row=streak,
    )
    # A snapshot is only created on a change, so the same change can never alert twice.
    # If the page changes on every check ("noisy"), stop alerting to avoid endless alerts.
    if new_status == "ok":
        send_alert(page, describe_change(last["text"], cleaned, last["id"], new_id))


# ---------------------------------------------------------------------------
# 8. Job queue + scheduler hook
# ---------------------------------------------------------------------------
job_queue = ThreadPoolExecutor(max_workers=2)  # at most 2 checks run at the same time
_in_progress = set()
_progress_lock = threading.Lock()


def submit_check(page_id):
    """Put a check in the queue (unless that page is already being checked)."""
    with _progress_lock:
        if page_id in _in_progress:
            return False
        _in_progress.add(page_id)
    job_queue.submit(_run_check, page_id)
    return True


def _run_check(page_id):
    try:
        check_page(page_id)
    except Exception as e:
        print("Check crashed:", e)
    finally:
        with _progress_lock:
            _in_progress.discard(page_id)


def run_due_checks():
    """Called every minute by the scheduler: queue every page whose interval has passed."""
    now = datetime.now(timezone.utc)
    for page in db.query("SELECT id, interval_minutes, last_checked FROM pages"):
        if page["last_checked"] is None:
            due = True
        else:
            last = datetime.fromisoformat(page["last_checked"])
            due = now - last >= timedelta(minutes=page["interval_minutes"])
        if due:
            submit_check(page["id"])
