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
  status TEXT DEFAULT 'new',  -- new | qualified | drafted | approved | contacted | replied | won | lost | do_not_contact | merged
  draft TEXT, source TEXT, notes TEXT,
  email TEXT, email_source TEXT,  -- publicly listed business email and where it was found
  also_on TEXT DEFAULT '',  -- other profiles of the same business: "platform:url; platform:url"
  merged_into INTEGER,       -- set on duplicate rows (status='merged') pointing at the kept lead
  created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS do_not_contact (url TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS emails (
  id INTEGER PRIMARY KEY, lead_id INTEGER, to_email TEXT, subject TEXT,
  resend_id TEXT, status TEXT,  -- sent | delivered | bounced | complained | failed
  error TEXT, sent_at TEXT,
  kind TEXT DEFAULT 'initial'   -- initial | showcase (the one follow-up, 7+ days after the first email)
);
CREATE TABLE IF NOT EXISTS previews (  -- a store preview built from a lead's public information
  lead_id INTEGER PRIMARY KEY, profile TEXT NOT NULL,  -- JSON: brand colour, logo info, products, notes
  status TEXT DEFAULT 'draft',                         -- draft | approved | sent | skipped
  approved_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS preview_images (  -- the logo and up to 3 product photos, stored so they never hotlink
  lead_id INTEGER NOT NULL, slot TEXT NOT NULL,        -- logo | p1 | p2 | p3
  content_type TEXT, data BLOB, source_url TEXT,
  PRIMARY KEY (lead_id, slot)
);
CREATE TABLE IF NOT EXISTS demo_sites (  -- temporary Netlify storefront for a lead's preview
  lead_id INTEGER PRIMARY KEY, slug TEXT NOT NULL, site_id TEXT, dns_zone_id TEXT, dns_record_id TEXT, url TEXT,
  status TEXT DEFAULT 'live',  -- live | deleted
  note TEXT, deployed_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS clients (  -- merchants whose store we built and handed over
  id INTEGER PRIMARY KEY, lead_id INTEGER, name TEXT NOT NULL, email TEXT NOT NULL, store_url TEXT,
  handed_over_at TEXT,                         -- YYYY-MM-DD (Philippine date)
  popload_status TEXT DEFAULT 'not_installed', -- not_installed | installed | active
  status TEXT DEFAULT 'active',                -- active | paused | done
  notes TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS followups (         -- one row per scheduled email per client
  id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL, step TEXT NOT NULL, due_at TEXT NOT NULL,
  status TEXT DEFAULT 'pending',               -- pending | sent | skipped | failed
  attempts INTEGER DEFAULT 0, sent_at TEXT, resend_id TEXT, note TEXT,
  UNIQUE(client_id, step)
);
CREATE TABLE IF NOT EXISTS prospects (  -- existing Shopify stores to offer POPLoad to (a separate track from store-build leads)
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, website TEXT NOT NULL, domain TEXT UNIQUE NOT NULL, niche TEXT, claim TEXT,
  platform TEXT DEFAULT 'unchecked',    -- unchecked | has_shopify | no_store | unreachable
  pay_level TEXT DEFAULT '', pay_methods TEXT DEFAULT '',  -- proof | mention | none, and the methods seen on the site
  name_auto INTEGER DEFAULT 0,                             -- 1 while the name is only a placeholder made from the domain
  pain TEXT DEFAULT '', pain_score INTEGER DEFAULT 0,      -- proof-of-payment habits seen on the site, and the fit score
  email TEXT, email_source TEXT, email_alts TEXT DEFAULT '',
  status TEXT DEFAULT 'new',  -- new | verified | needs_email | rejected | approved | replied | won | lost | done
  reject_reason TEXT, verified_at TEXT, approved_at TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS prospect_steps (  -- the POPLoad email sequence, one row per step
  id INTEGER PRIMARY KEY, prospect_id INTEGER NOT NULL, step TEXT NOT NULL,
  status TEXT DEFAULT 'pending',  -- pending | sent | skipped | failed
  attempts INTEGER DEFAULT 0, sent_at TEXT, resend_id TEXT, note TEXT,
  UNIQUE(prospect_id, step)
);
CREATE TABLE IF NOT EXISTS assistant_proposals (  -- things the assistant suggests; nothing runs until the owner confirms
  id INTEGER PRIMARY KEY, created_at TEXT, action TEXT NOT NULL, args TEXT NOT NULL, summary TEXT, reason TEXT,
  source TEXT DEFAULT 'chat',   -- chat | briefing
  status TEXT DEFAULT 'pending',  -- pending | done | dismissed | failed
  result TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS assistant_briefings (day TEXT PRIMARY KEY, payload TEXT NOT NULL, emailed_at TEXT);
CREATE TABLE IF NOT EXISTS assistant_chat (id INTEGER PRIMARY KEY, created_at TEXT, role TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS inbox_messages (  -- every message the inbox agent read (subjects and a summary, never the body)
  id INTEGER PRIMARY KEY, msg_id TEXT UNIQUE, ts TEXT, source TEXT,  -- source: email | website form
  addr TEXT, name TEXT, subject TEXT, category TEXT, priority TEXT, score INTEGER, summary TEXT,
  status TEXT,  -- processing | spam | notified | draft_waiting | auto_replied | opted_out | error
  matched TEXT, pending_id INTEGER, attempts INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS inbox_leads (  -- people who wrote to us (not the people we cold-emailed)
  email TEXT PRIMARY KEY, name TEXT, company TEXT, score INTEGER, category TEXT, first_ts TEXT, last_inbound_ts TEXT,
  last_outbound_ts TEXT, nurture_step INTEGER DEFAULT 0, stopped INTEGER DEFAULT 0, thread TEXT
);
CREATE TABLE IF NOT EXISTS inbox_pending (  -- reply and nurture drafts waiting for your Send or Skip
  id INTEGER PRIMARY KEY, email TEXT, subject TEXT, body TEXT, in_reply_to TEXT, kind TEXT,  -- reply | nurture
  status TEXT DEFAULT 'waiting', ts TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS suppressed_emails (email TEXT PRIMARY KEY, reason TEXT, added_at TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

PLATFORMS = ["facebook", "instagram", "tiktok", "shopee", "lazada", "carousell"]
MARKETPLACES = {"shopee", "lazada", "carousell", "tiktok"}
LABELS = {"facebook": "Facebook page", "instagram": "Instagram", "tiktok": "TikTok shop",
          "shopee": "Shopee shop", "lazada": "Lazada shop", "carousell": "Carousell shop",
          "web": "website", "maps": "business listing"}

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
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(leads)")}
    for col, ddl in (("also_on", "TEXT DEFAULT ''"), ("merged_into", "INTEGER"),
                     ("email", "TEXT"), ("email_source", "TEXT")):  # upgrade older DBs
        if col not in have:
            con.execute(f"ALTER TABLE leads ADD COLUMN {col} {ddl}")
    pcols = {r[1] for r in con.execute("PRAGMA table_info(prospects)")}
    for col, ddl in (("pain", "TEXT DEFAULT ''"), ("pain_score", "INTEGER DEFAULT 0"), ("name_auto", "INTEGER DEFAULT 0")):
        if col not in pcols:
            con.execute(f"ALTER TABLE prospects ADD COLUMN {col} {ddl}")
    if "kind" not in {r[1] for r in con.execute("PRAGMA table_info(emails)")}:
        con.execute("ALTER TABLE emails ADD COLUMN kind TEXT DEFAULT 'initial'")
    con.commit()
    return con


def upsert_lead(con, url, name="", snippet="", website="", source="", platform=None, email="", email_source=""):
    """Insert unless already known or on the do-not-contact list. Returns True if new.
    `platform` is for leads that are not social profiles (a business's own website, a map listing): `url` is then
    already canonical and is stored as given."""
    canon = url if platform else None
    if not platform:
        platform, canon = normalize(url)
    if not canon:
        return False
    if con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (canon,)).fetchone():
        return False
    if con.execute("SELECT 1 FROM leads WHERE url=?", (canon,)).fetchone():
        return False
    t = now()
    con.execute(
        "INSERT INTO leads (platform,url,name,snippet,website,source,email,email_source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (platform, canon, name, snippet, website, source, email or None, email_source or None, t, t))
    con.commit()
    return True


def add_do_not_contact(con, url, reason=""):
    canon = normalize(url)[1] or url
    con.execute("INSERT OR REPLACE INTO do_not_contact VALUES (?,?,?)", (canon, reason, now()))
    con.execute("UPDATE leads SET status='do_not_contact', updated_at=? WHERE url=?", (now(), canon))
    con.commit()
