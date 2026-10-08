import re
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY,
  platform TEXT DEFAULT 'facebook',  -- facebook | instagram | tiktok | shopee | lazada | carousell
  url TEXT UNIQUE NOT NULL,
  name TEXT, snippet TEXT, website TEXT,
  shopify_status TEXT DEFAULT 'unchecked',  -- unchecked | has_shopify | no_store | marketplace_only | unknown
  score INTEGER DEFAULT 0, score_notes TEXT,
  status TEXT DEFAULT 'new',  -- new | qualified | drafted | contacted | replied | won | lost | do_not_contact | merged
  draft TEXT, source TEXT, notes TEXT,
  also_on TEXT DEFAULT '',  -- other profiles of the same business: "platform:url; platform:url"
  merged_into INTEGER,       -- set on duplicate rows (status='merged') pointing at the kept lead
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS do_not_contact (url TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
"""

PLATFORMS = ["facebook", "instagram", "tiktok", "shopee", "lazada", "carousell"]
MARKETPLACES = {"shopee", "lazada", "carousell", "tiktok"}
LABELS = {"facebook": "Facebook page", "instagram": "Instagram", "tiktok": "TikTok shop",
          "shopee": "Shopee shop", "lazada": "Lazada shop", "carousell": "Carousell shop"}

_SKIP = {
    "facebook": {"groups", "events", "watch", "marketplace", "photo", "photos", "posts", "story.php",
                 "sharer", "share", "login", "reel", "hashtag", "public"},
    "instagram": {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "directory", "about"},
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(url):
    """Return (platform, canonical_profile_url) or (None, None) if it isn't a seller profile."""
    u = url.strip()
    m = re.match(r"https?://(?:[\w-]+\.)?(facebook|fb)\.com/([^?#]*)", u, re.I)
    if m:
        parts = [p for p in m.group(2).split("/") if p]
        if not parts or parts[0].lower() in _SKIP["facebook"]:
            return None, None
        first = parts[0]
        if first.lower() == "profile.php":
            mid = re.search(r"id=(\d+)", u)
            return ("facebook", f"https://facebook.com/profile.php?id={mid.group(1)}") if mid else (None, None)
        if first.lower() == "pages" and len(parts) >= 3:
            return "facebook", f"https://facebook.com/pages/{parts[1]}/{parts[2]}"
        return "facebook", f"https://facebook.com/{first}"
    m = re.match(r"https?://(?:www\.)?instagram\.com/([^/?#]+)", u, re.I)
    if m and m.group(1).lower() not in _SKIP["instagram"]:
        return "instagram", f"https://instagram.com/{m.group(1).lower()}"
    m = re.match(r"https?://(?:www\.)?tiktok\.com/(@[^/?#]+)", u, re.I)
    if m:
        return "tiktok", f"https://tiktok.com/{m.group(1).lower()}"
    m = re.match(r"https?://(?:www\.)?shopee\.ph/([^/?#]+)(?:/(\d+))?", u, re.I)
    if m:
        slug = m.group(1)
        if slug.lower() == "shop" and m.group(2):
            return "shopee", f"https://shopee.ph/shop/{m.group(2)}"
        if "-i." not in slug and slug.lower() not in {"search", "mall", "m", "cart", "user", "buyer", "flash_sale", "daily_discover"} \
                and not slug.startswith(("list", "find_similar")):
            return "shopee", f"https://shopee.ph/{slug}"
        return None, None
    m = re.match(r"https?://(?:www\.)?lazada\.com\.ph/shop/([^/?#]+)", u, re.I)
    if m:
        return "lazada", f"https://lazada.com.ph/shop/{m.group(1).lower()}"
    m = re.match(r"https?://(?:www\.)?carousell\.ph/u/([^/?#]+)", u, re.I)
    if m:
        return "carousell", f"https://carousell.ph/u/{m.group(1).lower()}"
    return None, None


def normalize_fb_url(url):  # backwards-compatible helper
    p, n = normalize(url)
    return n if p == "facebook" else None


def connect(path):
    import os
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    for col, ddl in (("also_on", "TEXT DEFAULT ''"), ("merged_into", "INTEGER")):  # upgrade older DBs
        if col not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {col} {ddl}")
    con.commit()
    return con


def upsert_lead(con, url, name="", snippet="", website="", source=""):
    """Insert unless already known or on the do-not-contact list. Returns True if new."""
    platform, canon = normalize(url)
    if not canon:
        return False
    if con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (canon,)).fetchone():
        return False
    if con.execute("SELECT 1 FROM leads WHERE url=?", (canon,)).fetchone():
        return False
    t = now()
    con.execute(
        "INSERT INTO leads (platform,url,name,snippet,website,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (platform, canon, name, snippet, website, source, t, t))
    con.commit()
    return True


def add_do_not_contact(con, url, reason=""):
    canon = normalize(url)[1] or url
    con.execute("INSERT OR REPLACE INTO do_not_contact VALUES (?,?,?)", (canon, reason, now()))
    con.execute("UPDATE leads SET status='do_not_contact', updated_at=? WHERE url=?", (now(), canon))
    con.commit()
