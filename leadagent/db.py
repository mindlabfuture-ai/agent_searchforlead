import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  fb_url TEXT UNIQUE NOT NULL,
  name TEXT, snippet TEXT, website TEXT,
  shopify_status TEXT DEFAULT 'unchecked',  -- unchecked | has_shopify | no_store | marketplace_only | unknown
  score INTEGER DEFAULT 0, score_notes TEXT,
  status TEXT DEFAULT 'new',  -- new | qualified | drafted | contacted | replied | won | lost | do_not_contact
  draft TEXT, source TEXT, notes TEXT,
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS do_not_contact (fb_url TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_fb_url(url):
    """Canonical page URL: https://facebook.com/<slug>. Returns None if not a page-like URL."""
    m = re.match(r"https?://(?:[\w-]+\.)?(?:facebook|fb)\.com/([^?#]*)", url.strip(), re.I)
    if not m:
        return None
    parts = [p for p in m.group(1).split("/") if p]
    if not parts:
        return None
    first = parts[0].lower()
    if first in {"groups", "events", "watch", "marketplace", "photo", "photos", "posts",
                 "story.php", "sharer", "share", "login", "reel", "hashtag", "public"}:
        return None
    if first == "profile.php":
        mid = re.search(r"id=(\d+)", url)
        return f"https://facebook.com/profile.php?id={mid.group(1)}" if mid else None
    if first == "pages" and len(parts) >= 3:  # /pages/Name/12345
        return f"https://facebook.com/pages/{parts[1]}/{parts[2]}"
    return f"https://facebook.com/{parts[0]}"


def connect(path):
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def upsert_lead(con, fb_url, name="", snippet="", website="", source=""):
    """Insert a lead unless it exists or is on the do-not-contact list. Returns True if new."""
    url = normalize_fb_url(fb_url)
    if not url:
        return False
    if con.execute("SELECT 1 FROM do_not_contact WHERE fb_url=?", (url,)).fetchone():
        return False
    if con.execute("SELECT 1 FROM leads WHERE fb_url=?", (url,)).fetchone():
        return False
    t = now()
    con.execute(
        "INSERT INTO leads (fb_url,name,snippet,website,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
        (url, name, snippet, website, source, t, t))
    con.commit()
    return True


def add_do_not_contact(con, fb_url, reason=""):
    url = normalize_fb_url(fb_url) or fb_url
    con.execute("INSERT OR REPLACE INTO do_not_contact VALUES (?,?,?)", (url, reason, now()))
    con.execute("UPDATE leads SET status='do_not_contact', updated_at=? WHERE fb_url=?", (now(), url))
    con.commit()
