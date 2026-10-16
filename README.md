# Website Change Detector

A small service that watches public web pages and alerts you when something **meaningful** changes, while ignoring noise like timestamps, counters and ads.

Built for the VinnovateIT web development round (Task 2).

> **AI assistance disclosure:** This project was built with the help of an AI assistant (Claude, by Anthropic). I chose the task and kept the scope deliberately basic so the code stays simple enough for me to read and explain. The code, tests and this README were written with AI assistance.

---

## What it does

- Add a URL with a name and a check interval (15 minutes to 24 hours).
- A scheduler checks pages through a **job queue** (2 workers) with a 30 s timeout and 2 retries.
- Pages are rendered in a **headless browser (Playwright)**, so JavaScript pages work. Public pages only, no logins or stored credentials.
- Each check saves the page text and a screenshot. **A new version is stored only when the content hash changes.**
- **Noise filtering** removes dates, times, random tokens, scripts, ads, banners, cookie notices and carousels. A **sensitivity** setting controls how strict it is:
  - High: any text change counts
  - Medium: ignores dates, times and tokens
  - Low: also ignores all numbers
- **Watch / ignore regions** using CSS selectors (typed in, see "Limitations").
- **Diffs:** side-by-side text diff, plus a screenshot diff with changed areas highlighted in red. You can compare any two versions.
- **Alerts** by Discord webhook and/or email, with a summary and a link to the diff. No duplicate alerts.
- **Dashboard** with last checked, last changed and status (`ok`, `error`, `blocked`, `noisy`) and a version timeline per page.

## How to run

```bash
python -m venv venv
venv\Scripts\activate          # Windows   (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
playwright install chromium
python app.py
```

Open http://localhost:5000

Optional settings (environment variables):

| Variable | Purpose |
|---|---|
| `BASE_URL` | Public address used in alert links (default `http://localhost:5000`) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Turn on email alerts. If not set, email is skipped; Discord still works. |

Run the tests (they use a fake browser, so no internet needed):

```bash
python test_basic.py
```

## Project structure

| File | What it does |
|---|---|
| `app.py` | Flask web app, routes, and the scheduler |
| `checker.py` | Fetching, noise filtering, hashing, snapshots, alerts, job queue |
| `diffs.py` | Text diff and screenshot diff |
| `db.py` | SQLite tables and small helper functions |
| `templates/` | The HTML pages |
| `test_basic.py` | Basic tests |

## How the design questions are handled

**1. Is a site outage or CAPTCHA a change?**
No. HTTP errors and timeouts mark the page `error`. A 403/429 response or CAPTCHA wording marks it `blocked`. In both cases no snapshot is saved and no change alert is sent. Blocks are not retried, because retrying makes things worse.

**2. If 200 users watch the same URL, is it fetched 200 times?**
No. The `pages` table has one row per URL (UNIQUE). If someone adds a URL that already exists, they are added as another *subscriber* instead. Each URL is fetched once per interval, and the alert goes to every subscriber.

**3. How do we avoid endless alerts on pages that differ on every load?**
Three layers: (a) the noise filter strips dates, times, tokens and numbers, (b) users can ignore specific areas with CSS selectors, and (c) if a page changes on 5 checks in a row it is marked `noisy` and alerts are paused until it has a stable check again.

**4. How do we avoid hammering a site?**
The minimum interval is 15 minutes, only 2 checks run at a time, and there is a 10 second minimum gap between requests to the same website. Every URL is fetched once no matter how many people watch it, and blocked responses are not retried.

## Limitations (what is simplified, honestly)

- **Visual picker:** instead of clicking regions on the page, you type CSS selectors for "watch" and "ignore" areas. They are saved per page.
- **Alerts:** Discord webhook and email only. No Telegram, and no optional daily digest.
- **No user accounts:** anyone who opens the app can see all pages. Subscribers are just an email and/or webhook attached to a page.
- **Settings are per URL:** if a URL is already watched, the first person's interval and selectors are kept.
- **Screenshots** show the top of the page only (1280x800), not the full page.
- **Noise filter** is rule-based. A banner or cookie notice with an unusual class name might not be removed, and a hero section with "banner" in its class name would be removed.
- **Private-address check** happens when a URL is added; it does not follow redirects.
- **CAPTCHA detection** looks for keywords, so it can have false positives and false negatives.
- No bonus features (keyword alerts, number charts, AI summaries, extension, RSS).
- Built for local use. It is not hardened for production (dev secret key, Flask development server).

## Tech

Python, Flask, Playwright, APScheduler, SQLite, Pillow, difflib.
