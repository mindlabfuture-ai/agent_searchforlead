"""The one follow-up to a cold email: a store preview sent 7+ days after the first email.

Rules, all enforced here:
  * at most TWO emails per business ever: the first, and one showcase;
  * the showcase goes out only if the first email was sent 7+ days ago, the lead is still `contacted`
    (mark a lead `replied` or `do_not_contact` and it stops), the address is not suppressed, and YOU approved
    that lead's preview after looking at it;
  * same business-hours window, daily cap, unsubscribe link and suppression as every other email."""
import re
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from . import demosite, config, db, emailing, emailtemplate, previews

DAYS_UNTIL_SHOWCASE = 7
PREP_DAYS = 5          # previews are built a couple of days early so they are ready to review on day 7
PREP_PER_RUN = 3       # building one fetches a website and a few images, so only a few per scheduler pass


def first_email_at(con, lead_id):
    r = con.execute("SELECT MIN(sent_at) FROM emails WHERE lead_id=? AND kind='initial' AND status!='failed'", (lead_id,)).fetchone()
    if not (r and r[0]):
        return None
    dt = datetime.fromisoformat(r[0])
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def showcase_sent(con, lead_id):
    return bool(con.execute("SELECT 1 FROM emails WHERE lead_id=? AND kind='showcase' AND status!='failed'", (lead_id,)).fetchone())


def days_since_first(con, lead_id, now=None):
    first = first_email_at(con, lead_id)
    return None if first is None else ((now or datetime.now(timezone.utc)) - first).days


def is_due(con, lead, now=None):
    d = days_since_first(con, lead["id"], now)
    return lead["status"] == "contacted" and d is not None and d >= DAYS_UNTIL_SHOWCASE and not showcase_sent(con, lead["id"])


def can_send(con, lead, now=None):
    """(ok, reason). The preview must be approved on top of everything is_due checks."""
    if lead["status"] != "contacted":
        return False, "lead is not in the contacted state"
    if not is_due(con, lead, now):
        return False, "not due (needs 7+ days since the first email and no showcase sent yet)"
    pv = previews.load(con, lead["id"])
    if not pv or pv["status"] != "approved":
        return False, "preview not approved"
    email = (lead["email"] or "").lower()
    if not email:
        return False, "no email"
    if emailing.is_suppressed(con, email):
        return False, "suppressed"
    if con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (lead["url"],)).fetchone():
        return False, "do not contact"
    if con.execute("SELECT COUNT(*) FROM emails WHERE lead_id=? AND kind='showcase' AND status='failed'", (lead["id"],)).fetchone()[0] >= emailing.MAX_FAILURES:
        return False, "too many failures"
    return True, ""


def build_showcase(lead, preview, base_url, demo_url=""):
    name = " ".join((lead["name"] or "").split())[:60]
    shop = name or "your"
    company = config.env("SENDER_COMPANY", "MindLab Future AI")
    sender = config.env("SENDER_NAME", "Mark")
    address = config.env("SENDER_ADDRESS", "Corporate Tower 2, BGC, Taguig City, Philippines")
    reply = config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com")
    greeting = f"Hi {name} team," if name else "Hi there,"
    subject = f"A preview of your {name} store" if name else "A preview of your store"
    version = re.sub(r"\D", "", preview["updated_at"] or "")[:14] or "0"
    unsub = f"{base_url}/unsubscribe?t={emailing.unsub_token(lead['email'])}"
    source = lead["email_source"] or "your public listing"
    why = f"You're getting this follow-up because I wrote to you last week and this business address is publicly listed at {source}."
    intro = "A week ago I wrote about building you a free Shopify store. I went ahead and put together a quick preview so you can see what it could look like:"
    logo_note = ("Logo: the one from your own page or website." if preview["logo"]["kind"] == "image"
                 else "Logo: a simple placeholder I made. Send me yours and I'll use it.")
    sample = any(p["sample"] for p in preview["products"])
    products_note = ("Products: samples only. I'd set up yours." if sample else "Products: taken from your own website or the ones you shared.")
    notes = [f"Colors: {preview['brand_note']}.", logo_note, products_note]
    closing = "If you'd like me to build it, just reply and I'll start. If not, no problem, and I won't email you again."
    disclosure = "As a Shopify Partner I may earn a referral fee from Shopify when you subscribe. It costs you nothing extra."
    prod_lines = "\n".join(f"- {p['name']}{' ' + p['price'] if p['price'] else ''}{' (sample)' if p['sample'] else ''}" for p in preview["products"])
    text = (f"{greeting}\n\n{intro}\n\n[Store preview for {preview['name']}: {preview['slug']}.mindlabfuture-ai.com. "
            f"The picture is in the HTML version of this email.]\n{prod_lines}\n\nWhat you're seeing:\n" + "\n".join(f"- {n}" for n in notes) +
            (f"\n\nClick through it on your phone (a design preview, not a live store): {demo_url}" if demo_url else "") + f"\n\n{closing}\nJust reply to this email.\n\n{disclosure}\n\n{sender}\n{company}"
            f"\n\n--\n{why}\n{company}, {address}\nNot interested? Unsubscribe: {unsub} (or just reply STOP).")
    mockup = previews.render_mockup(preview, base_url, lead["id"], version)
    html = emailtemplate.render_showcase(
        subject=subject, preheader=f"A quick preview of the store I could build for {name or 'you'}.", greeting=greeting, intro=intro,
        mockup_html=mockup, notes=notes, closing=closing, disclosure=disclosure, cta_label="Yes, build my store",
        cta_mailto=f"mailto:{reply}?subject={quote('Build my store: ' + (name or 'my shop'))}", signature=[sender, company],
        unsub_url=unsub, why=why, company=company, address=address, logo_url=config.env("LOGO_URL", emailtemplate.LOGO_URL), demo_url=demo_url)
    return {"subject": subject, "text": text, "html": html,
            "headers": {"List-Unsubscribe": f"<{unsub}>, <mailto:{reply}?subject=unsubscribe>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


def send_one(con, lead, now=None, post=None, base_url=None, enabled=None, log=print):
    """'sent' | 'dry_run' | 'failed' | 'skipped: why'."""
    ok, why = can_send(con, lead, now)
    if not ok:
        return f"skipped: {why}"
    enabled = config.env("EMAIL_SENDING_ENABLED").lower() == "true" if enabled is None else enabled
    base_url = base_url or config.base_url()
    if not base_url:
        return "skipped: BASE_URL not set (needed for the unsubscribe link and preview images)"
    pv = previews.load(con, lead["id"])
    msg = build_showcase(lead, pv, base_url, demosite.live_url(con, lead["id"]) or "")
    if not enabled:
        log(f"DRY RUN showcase -> {lead['email']}: {msg['subject']}")
        return "dry_run"
    sender = config.env("SENDER_NAME", "Mark")
    payload = {"from": f"{sender} <{config.env('SENDER_FROM_EMAIL')}>", "to": [lead["email"]],
               "reply_to": config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com"),
               "subject": msg["subject"], "text": msg["text"], "html": msg["html"], "headers": msg["headers"]}
    send = post or (lambda p, k: emailing.resend_post(p, config.env("RESEND_API_KEY"), k))
    try:
        res = send(payload, f"lead-{lead['id']}-showcase")
    except (urllib.error.URLError, OSError, ValueError) as e:
        con.execute("INSERT INTO emails (lead_id,to_email,subject,status,error,sent_at,kind) VALUES (?,?,?,?,?,?,'showcase')",
                    (lead["id"], lead["email"], msg["subject"], "failed", str(e)[:300], db.now()))
        con.commit()
        return "failed"
    con.execute("INSERT INTO emails (lead_id,to_email,subject,resend_id,status,sent_at,kind) VALUES (?,?,?,?,?,?,'showcase')",
                (lead["id"], lead["email"], msg["subject"], res.get("id"), "sent", db.now()))
    con.execute("UPDATE previews SET status='sent', updated_at=? WHERE lead_id=?", (db.now(), lead["id"]))
    con.commit()
    return "sent"


def run(con, post=None, now=None, force=False, enabled=None, base_url=None, log=print, sleep=None):
    """Send approved, due showcases within today's remaining cap. Returns the number sent."""
    now = now or datetime.now(timezone.utc)
    if not force and not emailing.in_send_window(now):
        return 0
    room = config.env_int("EMAIL_DAILY_CAP", 20) - emailing.sent_today(con, now)  # one cap shared with first emails
    sent = 0
    for lead in con.execute("SELECT l.* FROM leads l JOIN previews p ON p.lead_id=l.id WHERE l.status='contacted' AND p.status='approved' ORDER BY l.id").fetchall():
        if room <= 0:
            break
        result = send_one(con, lead, now, post, base_url, enabled, log)
        if result.startswith("skipped"):
            continue
        log(f"{result}: showcase for {lead['name'] or lead['url']}")
        if result == "sent":
            sent += 1
            room -= 1
            if sleep:
                sleep(60)
    return sent


def prepare(con, now=None, limit=PREP_PER_RUN, fetch_html=None, fetch_image=None, log=print):
    """Build previews, a couple of days early, for contacted leads that have none yet. Never approves or sends."""
    now = now or datetime.now(timezone.utc)
    built = 0
    for lead in con.execute("SELECT * FROM leads WHERE status='contacted' ORDER BY id").fetchall():
        if built >= limit:
            break
        d = days_since_first(con, lead["id"], now)
        if d is None or d < PREP_DAYS or showcase_sent(con, lead["id"]) or previews.load(con, lead["id"]):
            continue
        try:
            previews.generate(con, lead, fetch_html, fetch_image)
            built += 1
            log(f"built preview for {lead['name'] or lead['url']}")
        except Exception as e:  # one bad site must not stop the rest
            log(f"preview failed for {lead['url']}: {e!r}")
    return built
