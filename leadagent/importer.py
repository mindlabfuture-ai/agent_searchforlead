"""Add leads by hand: one URL or a pasted CSV from the dashboard, or a CSV file from the CLI."""
import csv
import io
import re

from . import db, emailing

MAX_ROWS = 200


def add_lead(con, url, name="", snippet="", website="", email="", source="dashboard"):
    """Returns (status, note) with status 'added' or 'skipped'."""
    url, name, snippet, website, email = (str(v or "").strip() for v in (url, name, snippet, website, email))
    platform, canon = db.normalize(url)
    if not canon:
        return "skipped", "not a supported profile URL (Facebook page, Instagram, TikTok, Shopee, Lazada or Carousell)"
    if con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (canon,)).fetchone():
        return "skipped", "opted out earlier"
    if con.execute("SELECT 1 FROM leads WHERE url=?", (canon,)).fetchone():
        return "skipped", "already in the list"
    if website and not re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", website, re.I):
        website = ""  # not a usable address; the rest of the lead is still fine
    found = emailing.extract_emails(email)
    if found and emailing.is_suppressed(con, found[0]):
        return "skipped", "that email address opted out or bounced"
    db.upsert_lead(con, canon, name, snippet, website, source=source)
    note = ""
    if found:
        con.execute("UPDATE leads SET email=?, email_source='added manually' WHERE url=?", (found[0], canon))
        con.commit()
    elif email:
        note = "email ignored (not a usable business address)"
    return "added", note


def parse_csv(text):
    """Rows as dicts with url,name,snippet,website,email. Header optional if the first column is a URL."""
    rows = [r for r in csv.reader(io.StringIO(text or "")) if any(c.strip() for c in r)]
    if not rows:
        return []
    cols = ["url", "name", "snippet", "website", "email"]
    if db.normalize(rows[0][0].strip())[1]:
        return [dict(zip(cols, r)) for r in rows]
    head = [h.strip().lower().replace("fb_url", "url").replace("link", "url") for h in rows[0]]
    return [dict(zip(head, r)) for r in rows[1:]]


def add_rows(con, rows, source="dashboard"):
    """Returns (results, truncated): results is a list of (label, status, note)."""
    results = []
    for r in rows[:MAX_ROWS]:
        status, note = add_lead(con, r.get("url", ""), r.get("name", ""), r.get("snippet", ""),
                                r.get("website", ""), r.get("email", ""), source)
        results.append(((r.get("name") or r.get("url") or "").strip(), status, note))
    return results, len(rows) > MAX_ROWS
