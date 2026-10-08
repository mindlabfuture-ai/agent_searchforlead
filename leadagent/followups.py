"""Follow-up emails to clients after a store is handed over: welcome (day 0), POPLoad check (day 7),
growth tips (day 30) and a next-step offer (day 60). These go to merchants who asked for the work, so they
send on a schedule without per-email approval, but every one still has the unsubscribe link, honours
suppression, sends only in business hours, and has a daily cap and spacing rules."""
import random
import re
import urllib.error
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone

from . import config, db, emailing, emailtemplate

STEPS = [("welcome", 0), ("popload_check", 7), ("growth", 30), ("next_level", 60)]
MIN_GAP_DAYS = 3    # never two follow-ups to one client within this many days
MAX_LATE_DAYS = 7   # a step this overdue is skipped, not sent late (e.g. after an outage or a long pause)
MAX_ATTEMPTS = 3
POPLOAD_STATUSES = ("not_installed", "installed", "active")

# ---- copy (English only): one version per step. Items written "Label: text" get a bold lead-in in the HTML. ----
COPY = {
    "greeting": "Hi {name} team,",
    "plain_cta": "Just reply to this email.",
    "welcome": dict(
        subject="Your store is yours: 3 things to do first",
        preheader="Congratulations on your new Shopify store. Here's what to do in the first week.",
        intro="Congratulations, your Shopify store is now yours{store}. Here is how to get the most out of the first week.",
        list_title="Do these first",
        items=["Add your payment details: put your GCash, Maya or bank account details in POPLoad so customers know where to pay.",
               "Try it yourself: place a test order, pay it, upload the receipt and approve it, so you have seen the whole flow.",
               "Share your link: pin the store on your Facebook page and put it in your Instagram bio."],
        items_no_popload=["Switch on POPLoad: it isn't active on your store yet. Reply POPLOAD and we'll set it up together.",
                          "Get your payment details ready: have your GCash, Maya or bank account details at hand for the setup.",
                          "Share your link: pin the store on your Facebook page and put it in your Instagram bio."],
        closing="If anything looks off, just reply to this email. I read every reply.",
        cta="Reply with a question", cta_subject="Question about my store"),
    "popload_check": dict(
        subject="Is POPLoad working for your customers?",
        preheader="A quick 3-step check that payments and receipts work.",
        intro="It has been a week since your store went live. Here is a 3-step check that payments work from start to finish:",
        list_title="Check it works",
        items=["Order: open your store on your phone, add a product and choose bank transfer, GCash or Maya.",
               "Receipt: upload a receipt on the thank-you page.",
               "Approve: open POPLoad in your Shopify admin and approve it. The order should be marked as paid."],
        closing="If a step fails, reply with a screenshot and I'll fix it. And when your first real order comes in, I'd love to hear about it.",
        cta="Tell me how it went", cta_subject="POPLoad check"),
    "growth": dict(
        subject="30 days in: 4 ways to get more orders",
        preheader="Simple things that bring first orders to a new store.",
        intro="Your store is a month old. These four things bring orders to most new stores:",
        list_title="Try these",
        items=["Real products: have at least 10 products with clear photos, prices and a short description.",
               "Your link everywhere: Facebook page button, pinned post, Instagram bio, and every \"how to order?\" reply.",
               "First reviews: ask your three best customers to order through the store and leave feedback.",
               "Make paying easy: say it in your posts, \"pay by GCash, upload your receipt, done\"."],
        closing="Would you reply with one line about working with me? It helps other small sellers find us.",
        cta="Send a quick review", cta_subject="My review"),
    "next_level": dict(
        subject="Ready for the next step with your store?",
        preheader="A few ways I can help you grow from here. No pressure.",
        intro="You are two months in. If sales are steady, here are a few ways I can help next. Reply with a number, no pressure.",
        list_title="Options",
        items=["Custom touches: banners, collections and a better product page layout.",
               "Automations: order updates and follow-up messages to repeat customers by email or SMS.",
               "VIPriority: for higher-value items like watches, jewelry and bags. VIP customer ranking, reservation fees and QR authenticity certificates. Early access is open.",
               "A 15-minute call: we look at your numbers and plan the next 60 days."],
        closing="Not now? That's fine too. Your store is yours either way.",
        cta="Reply with a number", cta_subject="Next step for my store"),
}


def today_pht(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(emailing.PHT).date()


# ---------- clients ----------
def add_client(con, name, email, store_url="", handed_over=None, lead_id=None,
               popload_status="not_installed", today=None):
    """Create a client and schedule every step. Returns (client_id, None) or (None, reason)."""
    name = " ".join(str(name or "").split())[:80]
    found = emailing.extract_emails(str(email or ""))
    store_url = str(store_url or "").strip()
    if not name:
        return None, "a business name is required"
    if not found:
        return None, "a usable business email is required"
    email = found[0]
    if emailing.is_suppressed(con, email):
        return None, "that address opted out or bounced"
    if con.execute("SELECT 1 FROM clients WHERE lower(email)=?", (email,)).fetchone():
        return None, "that email is already a client"
    if store_url and not re.match(r"^https://[\w.-]+\.[a-z]{2,}(/\S*)?$", store_url, re.I):
        return None, "store link must start with https://"
    if popload_status not in POPLOAD_STATUSES:
        return None, "unknown POPLoad status"
    today = today or today_pht()
    try:
        start = date.fromisoformat(str(handed_over)) if handed_over else today
    except ValueError:
        return None, "handover date must look like 2026-10-08"
    cur = con.execute("INSERT INTO clients (lead_id,name,email,store_url,handed_over_at,popload_status,created_at) "
                      "VALUES (?,?,?,?,?,?,?)", (lead_id, name, email, store_url, start.isoformat(), popload_status, db.now()))
    cid = cur.lastrowid
    for step, days in STEPS:
        due = start + timedelta(days=days)
        late = (today - due).days > MAX_LATE_DAYS
        con.execute("INSERT INTO followups (client_id,step,due_at,status,note) VALUES (?,?,?,?,?)",
                    (cid, step, due.isoformat(), "skipped" if late else "pending", "already too late when added" if late else None))
    if lead_id:
        con.execute("UPDATE leads SET status='won', updated_at=? WHERE id=?", (db.now(), lead_id))
    con.commit()
    return cid, None


def apply_action(con, client_id, action, today=None):
    """Dashboard actions on a client. Unknown or invalid actions change nothing."""
    c = con.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
    if not c:
        return
    today = today or today_pht()
    if action == "pause" and c["status"] == "active":
        con.execute("UPDATE clients SET status='paused' WHERE id=?", (client_id,))
    elif action == "resume" and c["status"] == "paused":
        con.execute("UPDATE clients SET status='active' WHERE id=?", (client_id,))
        # steps that came due while paused restart tomorrow, spaced out, instead of being skipped as late
        late = con.execute("SELECT id FROM followups WHERE client_id=? AND status='pending' AND due_at<? ORDER BY due_at",
                           (client_id, today.isoformat())).fetchall()
        for n, r in enumerate(late, 1):
            con.execute("UPDATE followups SET due_at=? WHERE id=?",
                        ((today + timedelta(days=1 + (n - 1) * MIN_GAP_DAYS)).isoformat(), r["id"]))
    elif action == "done":
        con.execute("UPDATE clients SET status='done' WHERE id=?", (client_id,))
        con.execute("UPDATE followups SET status='skipped', note='client marked done' WHERE client_id=? AND status='pending'", (client_id,))
    elif action in ("popload_installed", "popload_active"):
        con.execute("UPDATE clients SET popload_status=? WHERE id=?", (action.split("_", 1)[1], client_id))
    elif action == "skip_next":
        nxt = con.execute("SELECT id FROM followups WHERE client_id=? AND status='pending' ORDER BY due_at LIMIT 1", (client_id,)).fetchone()
        if nxt:
            con.execute("UPDATE followups SET status='skipped', note='skipped by owner' WHERE id=?", (nxt["id"],))
    con.commit()


# ---------- the message ----------
def build_followup(client, step, base_url):
    c, g = COPY[step], COPY
    name = " ".join((client["name"] or "").split())[:60]
    store = f" ({client['store_url']})" if client["store_url"] else ""
    items = c["items"]
    if step == "welcome" and client["popload_status"] == "not_installed":
        items = c["items_no_popload"]
    company = config.env("SENDER_COMPANY", "MindLab Future AI")
    sender = config.env("SENDER_NAME", "Mark")
    address = config.env("SENDER_ADDRESS", "Corporate Tower 2, BGC, Taguig City, Philippines")
    reply = config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com")
    unsub = f"{base_url}/unsubscribe?t={emailing.unsub_token(client['email'])}"
    why = "You're getting this because MindLab Future AI built your Shopify store."
    greeting, intro = (g["greeting"].format(name=name) if name else "Hi there,"), c["intro"].format(store=store)
    numbered = "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1))
    text = (f"{greeting}\n\n{intro}\n\n{c['list_title']}:\n{numbered}\n\n{c['closing']}\n{g['plain_cta']}\n\n{sender}\n{company}"
            f"\n\n--\n{why}\n{company}, {address}\nNot interested? Unsubscribe: {unsub} (or just reply STOP).")
    html = emailtemplate.render_followup(
        subject=c["subject"], preheader=c["preheader"], greeting=greeting, intro=intro, list_title=c["list_title"],
        items=items, closing=c["closing"], cta_label=c["cta"],
        cta_mailto=f"mailto:{reply}?subject={quote(c['cta_subject'])}", signature=[sender, company],
        unsub_url=unsub, why=why, company=company, address=address,
        logo_url=config.env("LOGO_URL", emailtemplate.LOGO_URL))
    return {"subject": c["subject"], "text": text, "html": html,
            "headers": {"List-Unsubscribe": f"<{unsub}>, <mailto:{reply}?subject=unsubscribe>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


# ---------- sending ----------
def sent_today(con, now=None):
    start = emailing.pht_day_start(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return con.execute("SELECT COUNT(*) FROM followups WHERE status='sent' AND sent_at>=?", (start,)).fetchone()[0]


def _last_sent(con, client_id):
    r = con.execute("SELECT MAX(sent_at) FROM followups WHERE client_id=? AND status='sent'", (client_id,)).fetchone()
    return datetime.fromisoformat(r[0]) if r and r[0] else None


def run(con, post=None, now=None, force=False, enabled=None, base_url=None, log=print, sleep=None):
    """Send every follow-up that is due. Returns the number sent. Dry run unless sending is enabled."""
    now = now or datetime.now(timezone.utc)
    if not force and not emailing.in_send_window(now):
        return 0
    enabled = config.env("EMAIL_SENDING_ENABLED").lower() == "true" if enabled is None else enabled
    base_url = base_url or config.base_url()
    if not base_url:
        log("follow-ups: BASE_URL not set (needed for the unsubscribe link)")
        return 0
    today = today_pht(now)
    room = config.env_int("FOLLOWUP_DAILY_CAP", 20) - sent_today(con, now)
    due = con.execute("SELECT f.*, c.id AS cid FROM followups f JOIN clients c ON c.id=f.client_id "
                      "WHERE f.status='pending' AND c.status='active' AND f.due_at<=? ORDER BY f.due_at, f.id",
                      (today.isoformat(),)).fetchall()
    sent = 0
    for f in due:
        c = con.execute("SELECT * FROM clients WHERE id=?", (f["cid"],)).fetchone()
        if (today - date.fromisoformat(f["due_at"])).days > MAX_LATE_DAYS:
            _mark(con, f["id"], "skipped", "too late to send")
            continue
        if emailing.is_suppressed(con, c["email"]):  # unsubscribed or bounced: stop the whole sequence
            con.execute("UPDATE clients SET status='done' WHERE id=?", (c["id"],))
            con.execute("UPDATE followups SET status='skipped', note='address opted out' WHERE client_id=? AND status='pending'", (c["id"],))
            con.commit()
            continue
        if f["step"] == "popload_check" and c["popload_status"] == "active":
            _mark(con, f["id"], "skipped", "already using POPLoad")
            continue
        last = _last_sent(con, c["id"])
        if last and (now - last) < timedelta(days=MIN_GAP_DAYS):
            continue  # too soon after the previous one; try again tomorrow
        if room <= 0:
            break
        msg = build_followup(c, f["step"], base_url)
        if not enabled:
            log(f"DRY RUN follow-up '{f['step']}' -> {c['email']}: {msg['subject']}")
            continue
        sender = config.env("SENDER_NAME", "Mark")
        payload = {"from": f"{sender} <{config.env('SENDER_FROM_EMAIL')}>", "to": [c["email"]],
                   "reply_to": config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com"),
                   "subject": msg["subject"], "text": msg["text"], "html": msg["html"], "headers": msg["headers"]}
        send = post or (lambda p, k: emailing.resend_post(p, config.env("RESEND_API_KEY"), k))
        try:
            res = send(payload, f"client-{c['id']}-{f['step']}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            attempts = f["attempts"] + 1
            con.execute("UPDATE followups SET attempts=?, status=?, note=? WHERE id=?",
                        (attempts, "failed" if attempts >= MAX_ATTEMPTS else "pending", str(e)[:200], f["id"]))
            con.commit()
            log(f"follow-up failed ({c['email']}, {f['step']}): {e}")
            continue
        con.execute("UPDATE followups SET status='sent', sent_at=?, resend_id=? WHERE id=?",
                    (db.now(), res.get("id"), f["id"]))
        con.commit()
        sent += 1
        room -= 1
        log(f"sent follow-up '{f['step']}' to {c['email']}")
        if sleep:
            sleep(random.randint(20, 60))
    return sent


def _mark(con, followup_id, status, note):
    con.execute("UPDATE followups SET status=?, note=? WHERE id=?", (status, note, followup_id))
    con.commit()
