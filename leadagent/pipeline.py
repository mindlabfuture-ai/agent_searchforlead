"""The lead pipeline as plain functions, shared by the CLI and the Railway scheduler."""
import threading
from datetime import date

from . import config, db, dedupe, emailing, osm, outreach, places, scoring, search, shopify_check


WORK_LOCK = threading.Lock()  # the scheduler and dashboard imports must not run the pipeline at once


def check_all(con, log=print):
    """Shopify check on each new lead's own site; also picks up a publicly listed business email."""
    for r in con.execute("SELECT * FROM leads WHERE shopify_status='unchecked' AND status='new'").fetchall():
        status, html = shopify_check.inspect_website(r["website"], platform=r["platform"])
        con.execute("UPDATE leads SET shopify_status=?, updated_at=? WHERE id=?", (status, db.now(), r["id"]))
        if not r["email"] and status != "has_shopify":
            found = emailing.extract_emails(f"{r['snippet'] or ''} {html}")
            if found:
                src = r["website"] or "a public search result"
                con.execute("UPDATE leads SET email=?, email_source=? WHERE id=?", (found[0], src, r["id"]))
        log(f"{status:<17} [{r['platform']}] {r['name'] or r['url']}")
    con.commit()


def score_all(con):
    for r in con.execute("SELECT * FROM leads WHERE status IN ('new','qualified')").fetchall():
        sc, notes = scoring.score_lead(r)
        keep = sc > 0 and r["source"] in scoring.MANUAL_SOURCES
        st = "qualified" if sc >= scoring.QUALIFY_AT or keep else "new"
        con.execute("UPDATE leads SET score=?, score_notes=?, status=?, updated_at=? WHERE id=?",
                    (sc, notes, st, db.now(), r["id"]))
    con.commit()
    return con.execute("SELECT COUNT(*) FROM leads WHERE status='qualified'").fetchone()[0]


def draft_n(con, limit=25):
    for r in con.execute("SELECT * FROM leads WHERE status='qualified' ORDER BY score DESC LIMIT ?", (limit,)):
        con.execute("UPDATE leads SET draft=?, status='drafted', updated_at=? WHERE id=?",
                    (outreach.draft(r), db.now(), r["id"]))
    con.commit()


def todays_focus(today=None):
    """Rotate through niche x location so each day's few searches cover something new."""
    n = (today or date.today()).toordinal()
    niche = config.NICHES[n % len(config.NICHES)]
    loc = config.LOCATIONS[(n // len(config.NICHES)) % len(config.LOCATIONS)]
    return niche, loc


def process_new(con, log=print):
    """Check, score and draft whatever was just added (used after a dashboard or CLI import)."""
    with WORK_LOCK:
        check_all(con, log)
        score_all(con)
        draft_n(con)


def run_daily(con, log=print, today=None):
    with WORK_LOCK:
        _run_daily(con, log, today)


def _run_daily(con, log, today):
    if config.env("SERPER_API_KEY") or config.env("BRAVE_API_KEY"):
        niche, loc = todays_focus(today)
        log(f"daily search: {niche} / {loc}")
        search.run_auto(con, search.provider(), niche, loc,
                        max_queries=config.env_int("DAILY_SEARCH_QUERIES", 6), min_new=5, log=log)
        dedupe.run(con, log=log)
    else:
        log("no search key set; skipping discovery")
    found = 0
    if config.env("OSM_DISCOVERY", "true").lower() != "false":  # free; set OSM_DISCOVERY=false to turn off
        found += osm.discover(con, today=today, log=log)
    if config.env("GOOGLE_PLACES_API_KEY"):
        found += places.discover(con, today=today, log=log)
    if found:
        dedupe.run(con, log=log)
    check_all(con, log)
    log(f"{score_all(con)} qualified")
    draft_n(con)
