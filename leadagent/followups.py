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

# ---- copy (Taglish only): one version per step. Items written "Label: text" get a bold lead-in in the HTML. ----
COPY = {
    "greeting": "Hi po {name} team,",
    "plain_cta": "Mag-reply lang po sa email na ito.",
    "welcome": dict(
        subject="Sa inyo na po ang store ninyo: 3 gagawin muna",
        preheader="Congrats po sa bagong Shopify store ninyo. Ito ang mga gagawin sa unang linggo.",
        intro="Congrats po! Sa inyo na ang Shopify store ninyo{store}. Ito po ang mga gawin para masulit ang unang linggo.",
        list_title="Gawin muna ito",
        items=["Ilagay ang payment details: ilagay po ang GCash, Maya o bank account ninyo sa POPLoad para alam ng customers kung saan magbabayad.",
               "Subukan po ninyo: mag-test order, bayaran, mag-upload ng resibo at i-approve, para makita ang buong proseso.",
               "I-share ang link: i-pin ang store sa Facebook page ninyo at ilagay sa Instagram bio."],
        items_no_popload=["I-on ang POPLoad: hindi pa po ito active sa store ninyo. Mag-reply lang ng POPLOAD at sabay nating i-set up.",
                          "Ihanda ang payment details: ihanda po ang GCash, Maya o bank account ninyo para sa setup.",
                          "I-share ang link: i-pin ang store sa Facebook page ninyo at ilagay sa Instagram bio."],
        closing="Kung may mali o hindi malinaw, mag-reply lang po sa email na ito. Binabasa ko po ang bawat reply.",
        cta="Mag-reply ng tanong", cta_subject="Tanong tungkol sa store ko"),
    "popload_check": dict(
        subject="Gumagana na po ba ang POPLoad para sa customers ninyo?",
        preheader="3-step na check kung gumagana ang bayad at resibo.",
        intro="Isang linggo na po mula nang mag-live ang store ninyo. Ito ang 3-step na check kung gumagana ang bayaran mula simula hanggang dulo:",
        list_title="I-check kung gumagana",
        items=["Order: buksan ang store sa phone, mag-add ng product at piliin ang bank transfer, GCash o Maya.",
               "Resibo: mag-upload ng resibo sa thank-you page.",
               "Approve: buksan ang POPLoad sa Shopify admin at i-approve. Dapat mamarkahang bayad na ang order."],
        closing="Kung may hindi gumana, mag-reply po kayo ng screenshot at aayusin ko. At kapag may unang totoong order na, gusto ko pong marinig.",
        cta="Sabihin kung kumusta", cta_subject="POPLoad check"),
    "growth": dict(
        subject="30 araw na po: 4 na paraan para dumami ang orders",
        preheader="Mga simpleng paraan para magkaroon ng unang orders ang bagong store.",
        intro="Isang buwan na po ang store ninyo. Ito ang apat na bagay na nagdadala ng orders sa karamihan ng bagong store:",
        list_title="Subukan ito",
        items=["Totoong products: hindi bababa sa 10 products na may malinaw na photo, presyo at maikling description.",
               "Link kahit saan: Facebook page button, pinned post, Instagram bio, at sa bawat \"paano mag-order?\".",
               "Unang reviews: hilingin sa tatlong best customers ninyo na mag-order sa store at mag-iwan ng feedback.",
               "Gawing madali ang bayad: sabihin sa posts, \"bayad via GCash, mag-upload ng resibo, tapos na\"."],
        closing="Pwede po ba kayong mag-reply ng isang linya tungkol sa pagtatrabaho natin? Nakakatulong po ito para mahanap kami ng ibang maliliit na seller.",
        cta="Magpadala ng review", cta_subject="Review ko"),
    "next_level": dict(
        subject="Handa na po ba kayo sa susunod na hakbang?",
        preheader="Ilang paraan para tumulong sa paglago ng store ninyo. Walang pressure.",
        intro="Dalawang buwan na po ang store ninyo. Kung steady na ang sales, ito ang mga pwede kong itulong. Mag-reply lang ng numero, walang pressure.",
        list_title="Mga option",
        items=["Custom touches: banners, collections at mas magandang product page layout.",
               "Automations: order updates at follow-up messages sa repeat customers via email o SMS.",
               "VIPriority: para sa mas mahal na items tulad ng relo, alahas at bag. VIP customer ranking, reservation fees at QR authenticity certificates. Bukas ang early access.",
               "15-minutong call: titingnan natin ang numbers ninyo at magplano ng susunod na 60 araw."],
        closing="Hindi pa po ngayon? Okay lang po. Sa inyo pa rin ang store ninyo.",
        cta="Mag-reply ng numero", cta_subject="Susunod na hakbang"),
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
    name = " ".join((client["name"] or "").split())[:60] or "Shop Owner"
    store = f" ({client['store_url']})" if client["store_url"] else ""
    items = c["items"]
    if step == "welcome" and client["popload_status"] == "not_installed":
        items = c["items_no_popload"]
    company = config.env("SENDER_COMPANY", "MindLab Future AI")
    sender = config.env("SENDER_NAME", "Mark")
    address = config.env("SENDER_ADDRESS", "Corporate Tower 2, BGC, Taguig City, Philippines")
    reply = config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com")
    unsub = f"{base_url}/unsubscribe?t={emailing.unsub_token(client['email'])}"
    why = "Natanggap ninyo ito dahil ang MindLab Future AI ang gumawa ng Shopify store ninyo."
    greeting, intro = g["greeting"].format(name=name), c["intro"].format(store=store)
    numbered = "\n".join(f"{n}. {i}" for n, i in enumerate(items, 1))
    text = (f"{greeting}\n\n{intro}\n\n{c['list_title']}:\n{numbered}\n\n{c['closing']}\n{g['plain_cta']}\n\n{sender}\n{company}"
            f"\n\n--\n{why}\n{company}, {address}\nAyaw na po bang makatanggap? Unsubscribe: {unsub} (o mag-reply lang ng STOP).")
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
