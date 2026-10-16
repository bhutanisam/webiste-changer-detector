"""A few simple tests. Run with:  python test_basic.py
They use a fake browser, so no internet or Playwright browser is needed."""
import os
import tempfile

import db

# use a temporary database so we never touch the real one
db.DATA_DIR = tempfile.mkdtemp()
db.DB_PATH = os.path.join(db.DATA_DIR, "test.db")
db.init_db()

import checker  # noqa: E402  (imported after the db is redirected)

checker.wait_for_domain = lambda url: None   # no waiting in tests
alerts = []
checker.send_alert = lambda page, summary: alerts.append(summary)


def make_page(sensitivity="medium"):
    return db.execute(
        "INSERT INTO pages (name, url, interval_minutes, sensitivity) VALUES (?,?,?,?)",
        ("Test", f"https://example.com/{sensitivity}/{len(alerts)}/{os.urandom(2).hex()}", 60, sensitivity),
    )


def fake_site(text, code=200):
    """Replace the real browser with one that returns fixed text."""
    checker.fetch_page = lambda url, watch, ignore: (code, text, text, b"fake-png")


def count_snapshots(page_id):
    return len(db.query("SELECT id FROM snapshots WHERE page_id=?", (page_id,)))


def test_noise_filter():
    text = "Hello\nUpdated 7:58 PM on 16/10/2026\nViews: 1523"
    assert "7:58" not in checker.clean_text(text, "medium")
    assert "1523" in checker.clean_text(text, "medium")      # medium keeps plain numbers
    assert "1523" not in checker.clean_text(text, "low")     # low drops them
    assert "7:58" in checker.clean_text(text, "high")        # high keeps everything


def test_snapshot_only_on_real_change():
    page_id = make_page()
    fake_site("Price list\nApples 10\nUpdated 7:58 PM")
    checker.check_page(page_id)
    assert count_snapshots(page_id) == 1                      # first version saved

    fake_site("Price list\nApples 10\nUpdated 7:58 PM")
    checker.check_page(page_id)
    assert count_snapshots(page_id) == 1                      # same content: nothing new

    fake_site("Price list\nApples 10\nUpdated 9:15 AM")
    checker.check_page(page_id)
    assert count_snapshots(page_id) == 1                      # only a timestamp: ignored

    before = len(alerts)
    fake_site("Price list\nApples 10\nMangoes 20\nUpdated 9:20 AM")
    checker.check_page(page_id)
    assert count_snapshots(page_id) == 2                      # real change: new version
    assert len(alerts) == before + 1                          # and exactly one alert


def test_blocked_and_error_are_not_changes():
    page_id = make_page("high")
    fake_site("Normal page")
    checker.check_page(page_id)

    fake_site("Please verify you are human (CAPTCHA)")
    checker.check_page(page_id)
    assert db.query("SELECT status FROM pages WHERE id=?", (page_id,), one=True)["status"] == "blocked"
    assert count_snapshots(page_id) == 1

    checker.time.sleep = lambda s: None                       # skip retry waits
    fake_site("Server error", code=500)
    checker.check_page(page_id)
    assert db.query("SELECT status FROM pages WHERE id=?", (page_id,), one=True)["status"] == "error"
    assert count_snapshots(page_id) == 1


def test_page_that_changes_every_time_gets_paused():
    page_id = make_page("high")
    before = len(alerts)
    for i in range(7):
        fake_site(f"Always different {i}")
        checker.check_page(page_id)
    status = db.query("SELECT status FROM pages WHERE id=?", (page_id,), one=True)["status"]
    assert status == "noisy"
    assert len(alerts) - before == 4                          # alerts stop at the 5th change


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print("passed:", name)
