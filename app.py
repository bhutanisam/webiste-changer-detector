"""app.py - the web app (Flask) and the scheduler.

Run it with:   python app.py
Then open:     http://localhost:5000
"""
import os
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for

import checker
import db
import diffs

app = Flask(__name__)
app.secret_key = "dev-only-secret"  # only needed for flash messages

ALLOWED_INTERVALS = [15, 30, 60, 360, 720, 1440]  # minutes (15 min up to 24 h)


@app.template_filter("nice")
def nice(value):
    """Make a stored timestamp easier to read."""
    return value[:16].replace("T", " ") + " UTC" if value else "never"


# ---------------------------------------------------------------- dashboard
@app.route("/")
def index():
    pages = db.query(
        "SELECT pages.*, (SELECT COUNT(*) FROM subscribers WHERE page_id = pages.id) AS watchers "
        "FROM pages ORDER BY id DESC"
    )
    return render_template("index.html", pages=pages, intervals=ALLOWED_INTERVALS)


# ----------------------------------------------------------------- add page
@app.route("/add", methods=["POST"])
def add():
    name = request.form.get("name", "").strip()
    url = request.form.get("url", "").strip()
    watch = request.form.get("watch_selector", "").strip()
    ignore = request.form.get("ignore_selector", "").strip()
    sensitivity = request.form.get("sensitivity", "medium")
    webhook = request.form.get("discord_webhook", "").strip()
    email = request.form.get("email", "").strip()
    try:
        interval = int(request.form.get("interval", "60"))
    except ValueError:
        interval = 0

    # --- validation ---
    if not name or not url.startswith(("http://", "https://")):
        flash("Please enter a name and a full URL starting with http:// or https://", "error")
        return redirect(url_for("index"))
    if interval not in ALLOWED_INTERVALS:
        flash("Check interval must be between 15 minutes and 24 hours.", "error")
        return redirect(url_for("index"))
    if sensitivity not in ("low", "medium", "high"):
        sensitivity = "medium"
    if not checker.is_public_url(url):
        flash("Only public web pages are allowed (that address could not be reached or is private).", "error")
        return redirect(url_for("index"))

    # --- one row per URL, so 200 people watching = still 1 fetch ---
    existing = db.query("SELECT id FROM pages WHERE url=?", (url,), one=True)
    if existing:
        page_id = existing["id"]
        flash("That URL is already being watched. You were added as another watcher (no extra fetches).", "ok")
    else:
        page_id = db.execute(
            "INSERT INTO pages (name, url, interval_minutes, watch_selector, ignore_selector, sensitivity) "
            "VALUES (?,?,?,?,?,?)",
            (name, url, interval, watch, ignore, sensitivity),
        )
        checker.submit_check(page_id)  # take the first snapshot right away

    if webhook or email:
        db.execute(
            "INSERT INTO subscribers (page_id, discord_webhook, email) VALUES (?,?,?)",
            (page_id, webhook, email),
        )
    return redirect(url_for("page_detail", page_id=page_id))


# -------------------------------------------------------------- page detail
@app.route("/page/<int:page_id>")
def page_detail(page_id):
    page = db.query("SELECT * FROM pages WHERE id=?", (page_id,), one=True)
    if page is None:
        abort(404)
    snapshots = db.query("SELECT * FROM snapshots WHERE page_id=? ORDER BY id DESC", (page_id,))
    return render_template("page.html", page=page, snapshots=snapshots)


@app.route("/page/<int:page_id>/check", methods=["POST"])
def check_now(page_id):
    if checker.submit_check(page_id):
        flash("Check queued. Refresh in a few seconds.", "ok")
    else:
        flash("This page is already being checked.", "ok")
    return redirect(url_for("page_detail", page_id=page_id))


@app.route("/page/<int:page_id>/delete", methods=["POST"])
def delete(page_id):
    db.execute("DELETE FROM snapshots WHERE page_id=?", (page_id,))
    db.execute("DELETE FROM subscribers WHERE page_id=?", (page_id,))
    db.execute("DELETE FROM pages WHERE id=?", (page_id,))
    return redirect(url_for("index"))


# ------------------------------------------------------------------ compare
@app.route("/compare")
def compare_form():
    """The 'compare' form on the page detail screen sends ?a=..&b=.. here."""
    return redirect(url_for("compare", a=request.args.get("a", type=int), b=request.args.get("b", type=int)))


@app.route("/compare/<int:a>/<int:b>")
def compare(a, b):
    old = db.query("SELECT * FROM snapshots WHERE id=?", (a,), one=True)
    new = db.query("SELECT * FROM snapshots WHERE id=?", (b,), one=True)
    if not old or not new or old["page_id"] != new["page_id"]:
        abort(404)
    page = db.query("SELECT * FROM pages WHERE id=?", (old["page_id"],), one=True)

    table = diffs.text_diff_html(old["text"], new["text"])

    # highlighted screenshot diff
    highlighted = None
    try:
        os.makedirs(os.path.join(db.DATA_DIR, "diffs"), exist_ok=True)
        out_name = f"diffs/{a}_{b}.png"
        diffs.screenshot_diff(
            os.path.join(db.DATA_DIR, old["screenshot"]),
            os.path.join(db.DATA_DIR, new["screenshot"]),
            os.path.join(db.DATA_DIR, out_name),
        )
        highlighted = out_name
    except Exception as error:
        print("Screenshot diff failed:", error)

    return render_template("compare.html", page=page, old=old, new=new, table=table, highlighted=highlighted)


@app.route("/files/<path:filename>")
def files(filename):
    """Serve screenshots from the data folder."""
    return send_from_directory(db.DATA_DIR, filename)


# --------------------------------------------------------------------- main
if __name__ == "__main__":
    db.init_db()
    scheduler = BackgroundScheduler()
    # Every minute, look for pages that are due and put them in the job queue.
    scheduler.add_job(checker.run_due_checks, "interval", minutes=1, next_run_time=datetime.now())
    scheduler.start()
    app.run(port=5000, debug=False)  # debug=False so the scheduler is only started once
