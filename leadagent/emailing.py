"""Resend email outreach, built to stay on the right side of Resend's terms and PH privacy law:
one human-approved, personalized message per business, to a publicly listed business address,
with one-click unsubscribe, a physical address, bounce/complaint suppression and a daily cap."""
import base64
import hashlib
import hmac
import html as htmllib
import json
import random
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from . import config, db, emailtemplate, outreach

PHT = timezone(timedelta(hours=8))
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
JUNK_DOMAINS = {"example.com", "sentry.io", "wixpress.com", "domain.com", "email.com", "yourdomain.com",
                "shopify.com", "myshopify.com", "godaddy.com", "squarespace.com", "wix.com"}
JUNK_LOCAL = ("noreply", "no-reply", "donotreply", "postmaster", "abuse", "webmaster", "mailer-daemon")
JUNK_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js", ".woff", ".woff2")
MAX_FAILURES = 3


# ---------- finding addresses ----------
def extract_emails(text):
    """Publicly listed addresses in `text`, junk (images, no-reply, platform addresses) removed."""
    found = []
    for e in EMAIL_RE.findall(htmllib.unescape(text or "")):
        e = e.lower().strip(".")
        local, _, domain = e.partition("@")
        if e.endswith(JUNK_SUFFIX) or domain in JUNK_DOMAINS or local.startswith(JUNK_LOCAL):
            continue
        if e not in found:
            found.append(e)
    return found


# ---------- unsubscribe tokens (stateless, signed) ----------
def _secret():
    s = config.env("UNSUB_SECRET")
    if len(s) < 16:
        raise RuntimeError("Set UNSUB_SECRET (16+ random characters)")
    return s.encode()


def unsub_token(email):
    e = base64.urlsafe_b64encode(email.lower().encode()).decode().rstrip("=")
    return f"{e}.{hmac.new(_secret(), e.encode(), hashlib.sha256).hexdigest()[:32]}"


def parse_unsub_token(token):
    try:
        e, sig = (token or "").rsplit(".", 1)
        good = hmac.new(_secret(), e.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, good):
            return None
        return base64.urlsafe_b64decode(e + "=" * (-len(e) % 4)).decode()
    except Exception:
        return None


# ---------- suppression ----------
def is_suppressed(con, email):
    return bool(con.execute("SELECT 1 FROM suppressed_emails WHERE email=?", (email.lower(),)).fetchone())


def suppress(con, email, reason):
    """Never email this address again; opt the matching business out of every channel."""
    email = email.lower()
    con.execute("INSERT OR REPLACE INTO suppressed_emails VALUES (?,?,?)", (email, reason, db.now()))
    for r in con.execute("SELECT url FROM leads WHERE lower(email)=?", (email,)).fetchall():
        db.add_do_not_contact(con, r["url"], f"email {reason}")
    con.commit()


# ---------- message ----------
EN = """Hi {name} team,

I found {where} and liked what you're selling.

I'm {sender} from {company}, a Shopify Partner in Taguig. We set up simple online stores for Filipino sellers: product pages, GCash/Maya/bank transfer checkout and shipping, so customers can order without messaging back and forth.{extra}

{offer}

Would you like a free preview of what a basic store for {name} could look like? You would own the store and the account.

Just reply to this email and I'll send it over.

{sender}
{company}"""

TL = """Hi po {name} team,

Nakita ko po {where} at nagustuhan ko ang products ninyo.

Ako po si {sender} ng {company}, Shopify Partner sa Taguig. Tumutulong po kami mag-set up ng simpleng online store para sa Filipino sellers: product pages, GCash/Maya/bank transfer checkout at shipping, para hindi na po kailangan ng back-and-forth sa DM.{extra}

{offer}

Gusto po ba ninyong makita ang libreng preview ng basic store para sa {name}? Sa inyo po ang store at account.

Reply lang po kayo sa email na ito at ipapadala ko.

{sender}
{company}"""

# The offer. First line is the heading, each "- " line is a bullet (the HTML email turns these into a card),
# each "Note: " line is small print under the card. These are commitments made in your name: keep them
# exactly as you will honour them.
# The store is built as a Shopify "client transfer store" and handed over, which is how Shopify attributes the
# referral. Shopify says transferred stores are NOT eligible for promotions or free trials, so there is
# deliberately no "$1/month" claim here.
OFFER_EN = """Here's the offer:
- Free store build: I design and set up your store, with POPLoad (Basic plan, up to 50 payment-receipt uploads) so customers can pay by GCash, Maya or bank transfer and upload their receipt. When it's ready I hand it over and you own the store and the account.
- Your Shopify plan: you choose and pay for your plan directly when you take over the store. I'll recommend the one that fits, no upsell.
- Money-back guarantee: if your store makes no sales in its first 3 months, I'll refund the Shopify fees you paid.
Note: your own domain name (like yourshop.com) isn't included. You can buy one or connect one you already own. If you'd rather not, I can set your store up for free on a subdomain such as yourshop.mindlabfuture-ai.com, and you can switch to your own domain anytime.
Note: as a Shopify Partner I may earn a referral fee from Shopify when you subscribe. It costs you nothing extra."""

OFFER_TL = """Ito po ang offer:
- Libreng store build: ako po ang magdidisenyo at mag-se-set up ng store ninyo, kasama ang POPLoad (Basic plan, hanggang 50 receipt uploads) para makapagbayad ang customers via GCash, Maya o bank transfer at mag-upload ng resibo. Kapag ready na, ibibigay ko po ito sa inyo at kayo ang may-ari ng store at account.
- Shopify plan ninyo: kayo po ang pipili at magbabayad ng plan nang direkta sa Shopify kapag kayo na ang may hawak ng store. Irerekomenda ko po ang pinakaangkop, walang pilitan.
- Money-back guarantee: kung walang sales ang store ninyo sa unang 3 buwan, ire-refund ko po ang binayad ninyo sa Shopify.
Note: hindi po kasama ang sariling domain name (hal. yourshop.com). Pwede kayong bumili o gamitin ang meron na kayo. Kung ayaw po muna, libre ko pong i-set up ang store sa subdomain tulad ng yourshop.mindlabfuture-ai.com, at pwede kayong lumipat sa sariling domain anumang oras.
Note: bilang Shopify Partner, maaari po akong makatanggap ng referral fee mula sa Shopify kapag nag-subscribe kayo. Wala po itong dagdag na bayad sa inyo."""

EXTRA_EN =" A store of your own also means no marketplace fees, and your customers' details are yours."
EXTRA_TL = " Kapag may sariling store, walang marketplace fees at sa inyo po ang customer details."


def build_email(lead, base_url, now=None):
    # The name comes from the web: flatten whitespace so it can't break paragraphs or the subject line.
    name = " ".join((lead["name"] or "your shop").split())[:60] or "your shop"
    company = config.env("SENDER_COMPANY", "MindLab Future AI")
    sender = config.env("SENDER_NAME", "Mark")
    address = config.env("SENDER_ADDRESS", "Corporate Tower 2, BGC, Taguig City, Philippines")
    tl = outreach.is_taglish(lead)
    where = f"your {db.LABELS.get(lead['platform'], 'page')} ({lead['url']})"
    marketplace = lead["platform"] in db.MARKETPLACES
    extra = (EXTRA_TL if tl else EXTRA_EN) if marketplace else ""
    offer = OFFER_TL if tl else OFFER_EN
    body = (TL if tl else EN).format(name=name, where=where, sender=sender, company=company, extra=extra, offer=offer)
    unsub = f"{base_url}/unsubscribe?t={unsub_token(lead['email'])}"
    source = lead["email_source"] or "your public listing"
    footer = (f"\n\n--\nYou're getting this one-time message because this business address is publicly listed "
              f"at {source}.\n{company}, {address}\nNot interested? Unsubscribe: {unsub} (or just reply STOP).")
    text = body + footer
    reply = config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com")
    subject = f"A simple online store for {name}"
    # Paragraphs of the template: greeting, found-you, pitch, offer, ask, reply line, signature.
    greeting, found, pitch, offer_block, ask, reply_line, signature = body.split("\n\n")
    offer_title, *offer_lines = offer_block.split("\n")
    offer_items = [i[2:] for i in offer_lines if i.startswith("- ")]
    offer_notes = [i for i in offer_lines if i.startswith("Note: ")]  # limits, shown small under the card
    callout = ""
    if marketplace:  # show the marketplace point as its own callout rather than burying it in the pitch
        pitch, callout = pitch.replace(extra, ""), extra.strip()
    html = emailtemplate.render_html(
        subject=subject, greeting=greeting, found=found, pitch=pitch, ask=ask, reply=reply_line,
        signature=signature.split("\n"), callout=callout, offer_title=offer_title, offer_items=offer_items, offer_notes=offer_notes,
        preheader=("Libreng Shopify store build para sa GCash at Maya, may money-back guarantee." if tl else
                   "A free Shopify store build set up for GCash and Maya, with a money-back guarantee."),
        cta_label="Gusto ko ng libreng preview" if tl else "Get my free store preview",
        cta_mailto=emailtemplate.cta_mailto(reply, name, tl),
        unsub_url=unsub, source=source, company=company, address=address,
        logo_url=config.env("LOGO_URL", emailtemplate.LOGO_URL))
    return {
        "subject": subject,
        "text": text, "html": html,
        "headers": {"List-Unsubscribe": f"<{unsub}>, <mailto:{reply}?subject=unsubscribe>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
    }


# ---------- sending ----------
def resend_post(payload, key, idempotency_key):
    req = urllib.request.Request(
        "https://api.resend.com/emails", json.dumps(payload).encode(),
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
         "Idempotency-Key": idempotency_key, "User-Agent": "mindlab-leadagent/1.0"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def pht_day_start(now):
    n = now.astimezone(PHT)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def sent_today(con, now=None):
    start = pht_day_start(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return con.execute("SELECT COUNT(*) FROM emails WHERE status!='failed' AND sent_at>=?", (start,)).fetchone()[0]


def in_send_window(now=None):
    """Mon-Fri, 9:00-17:00 Philippine time: business hours for a B2B message."""
    n = (now or datetime.now(timezone.utc)).astimezone(PHT)
    return n.weekday() < 5 and 9 <= n.hour < 17


def can_send(con, lead):
    if lead["status"] != "approved":
        return False, "not approved"
    email = (lead["email"] or "").lower()
    if not email:
        return False, "no email"
    if is_suppressed(con, email):
        return False, "suppressed"
    if con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (lead["url"],)).fetchone():
        return False, "do not contact"
    if con.execute("SELECT 1 FROM emails WHERE (lead_id=? OR to_email=?) AND status!='failed'",
                   (lead["id"], email)).fetchone():
        return False, "already emailed"
    if con.execute("SELECT COUNT(*) FROM emails WHERE lead_id=? AND status='failed'", (lead["id"],)).fetchone()[0] >= MAX_FAILURES:
        return False, "too many failures"
    return True, ""


def send_one(con, lead, post=None, base_url=None, enabled=None, log=print):
    """Returns 'sent' | 'dry_run' | 'failed' | 'skipped: <why>'."""
    ok, why = can_send(con, lead)
    if not ok:
        return f"skipped: {why}"
    enabled = config.env("EMAIL_SENDING_ENABLED").lower() == "true" if enabled is None else enabled
    base_url = base_url or config.base_url()
    if not base_url:
        return "skipped: BASE_URL not set (needed for the unsubscribe link)"
    msg = build_email(lead, base_url)
    if not enabled:
        log(f"DRY RUN -> {lead['email']}: {msg['subject']}")
        return "dry_run"
    sender = config.env("SENDER_NAME", "Mark")
    payload = {"from": f"{sender} <{config.env('SENDER_FROM_EMAIL')}>", "to": [lead["email"]],
               "reply_to": config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com"),
               "subject": msg["subject"], "text": msg["text"], "html": msg["html"], "headers": msg["headers"]}
    post = post or (lambda p, k: resend_post(p, config.env("RESEND_API_KEY"), k))
    try:
        res = post(payload, f"lead-{lead['id']}-initial")
    except (urllib.error.URLError, OSError, ValueError) as e:
        con.execute("INSERT INTO emails (lead_id,to_email,subject,status,error,sent_at) VALUES (?,?,?,?,?,?)",
                    (lead["id"], lead["email"], msg["subject"], "failed", str(e)[:300], db.now()))
        con.commit()
        return "failed"
    con.execute("INSERT INTO emails (lead_id,to_email,subject,resend_id,status,sent_at) VALUES (?,?,?,?,?,?)",
                (lead["id"], lead["email"], msg["subject"], res.get("id"), "sent", db.now()))
    con.execute("UPDATE leads SET status='contacted', updated_at=? WHERE id=?", (db.now(), lead["id"]))
    con.commit()
    return "sent"


def run_sender(con, post=None, now=None, force=False, enabled=None, log=print, sleep=time.sleep):
    """Send approved leads up to today's remaining cap, spaced out. Returns number sent."""
    now = now or datetime.now(timezone.utc)
    if not force and not in_send_window(now):
        return 0
    remaining = config.env_int("EMAIL_DAILY_CAP", 20) - sent_today(con, now)
    if remaining <= 0:
        return 0
    leads = con.execute("SELECT * FROM leads WHERE status='approved' ORDER BY score DESC LIMIT ?",
                        (remaining,)).fetchall()
    sent = 0
    for lead in leads:
        result = send_one(con, lead, post=post, enabled=enabled, log=log)
        log(f"{result}: {lead['name'] or lead['url']}")
        if result == "sent":
            sent += 1
            sleep(random.randint(60, max(61, config.env_int("EMAIL_SPACING_SECONDS", 120))))
    return sent


# ---------- Resend webhooks ----------
def verify_svix(secret, headers, body, now=None, tolerance=300):
    """Resend signs webhooks with Svix: HMAC-SHA256 over '<id>.<timestamp>.<body>'."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("svix-id"), h.get("svix-timestamp"), h.get("svix-signature")
    if not (msg_id and ts and sigs and secret):
        return False
    try:
        if abs((now or time.time()) - int(ts)) > tolerance:
            return False
        key = base64.b64decode(secret.split("_", 1)[1]) if secret.startswith("whsec_") else secret.encode()
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(v == "v1" and hmac.compare_digest(sig, expected)
               for v, _, sig in (part.partition(",") for part in sigs.split()))


def handle_resend_event(con, event):
    """Bounces and spam complaints suppress the address for good."""
    kind, data = event.get("type", ""), event.get("data", {})
    rid, to = data.get("email_id"), [t.lower() for t in data.get("to", [])]
    status = {"email.delivered": "delivered", "email.bounced": "bounced", "email.complained": "complained"}.get(kind)
    if not status:
        return "ignored"
    if rid:
        con.execute("UPDATE emails SET status=? WHERE resend_id=?", (status, rid))
        con.commit()
    if status in ("bounced", "complained"):
        for addr in to:
            suppress(con, addr, status)
    return status
