"""POPLoad prospecting: existing Shopify stores that take GCash / bank transfer and could use receipt uploads.

A separate track from the store-build leads. Rows are imported from a list, then *verified* from the sites
themselves: the domain must load, the site must run on Shopify (POPLoad is a Shopify app), and a business email
must be published on the site's own pages. Nothing is guessed or taken from anywhere else. A person approves each
prospect before anything is sent; the three-step sequence then runs on its own and stops on a reply, an
unsubscribe, a bounce or a complaint."""
import csv
import io
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

from . import config, db, emailing, emailtemplate, search, shopify_check

MAX_ROWS = 300
PAGES = ["/", "/pages/contact", "/pages/contact-us", "/pages/payment", "/pages/payment-options", "/pages/faq", "/policies/refund-policy"]
TLDS = {"com", "ph", "net", "org", "co", "io", "biz", "shop", "store", "asia", "info", "me", "ph", "online"}
PLACEHOLDER = ("xxx", "example", "yourdomain", "yourname", "domain", "email", "name@", "user@", "test@", "sample")
ROLE_LOCALS = ("hello", "hi", "info", "support", "sales", "contact", "shop", "orders", "care", "customer", "help", "admin")
PROOF_RE = re.compile(r"proof of payment|payment proof|deposit slip|payment (screenshot|receipt|confirmation)|transfer (receipt|confirmation|proof)", re.I)
METHODS = [("GCash", r"\bg-?cash\b"), ("Maya", r"\b(pay)?maya\b"), ("bank transfer", r"\bbank (transfer|deposit)\b|\bbdo\b|\bbpi\b|\bunionbank\b|\bmetrobank\b")]

# step, days after the first email
STEPS = [("intro", 0), ("how_it_works", 4), ("last_note", 11)]
MAX_LATE_DAYS = 7
MAX_ATTEMPTS = 3

COPY = {
    "greeting": "Hi {name} team,",
    "intro": dict(
        subject="Fewer payment receipts to chase at {name}",
        preheader="Customers upload the receipt, you approve it in one click.",
        intro="I noticed {name} takes {methods}, so you probably get payment receipts by email, Messenger or Viber and match them to orders by hand. I built POPLoad, a Shopify app, to take that off your plate.",
        list_title="What POPLoad does",
        items=["Upload: customers pay by bank transfer, GCash or Maya, then upload the receipt right on the order page.",
               "Approve: you see it in your Shopify admin and approve it with one click. The order is marked as paid.",
               "Calm inbox: no more digging through emails and chats for proof of payment."],
        closing="POPLoad is still in Shopify's app review, so I am offering early access: I install it with you, and the first 10 receipt uploads are free. Want me to send the install link?",
        cta="Yes, send me the link", cta_subject="POPLoad early access"),
    "how_it_works": dict(
        subject="How POPLoad works, in 3 steps",
        preheader="What your customers and you would see.",
        intro="A quick follow-up in case my last email got buried. This is what POPLoad looks like for {name}:",
        list_title="The flow",
        items=["Order: the customer picks bank transfer, GCash or Maya at checkout and pays from their own app.",
               "Receipt: on the thank-you page they upload a screenshot of the receipt.",
               "Approve: you open POPLoad in your Shopify admin, check it and approve it. The order shows as paid."],
        closing="If you would like to see it on your own store, reply \"demo\" and I will set it up with a test order.",
        cta="Reply \"demo\"", cta_subject="POPLoad demo"),
    "last_note": dict(
        subject="Should I close this out?",
        preheader="My last note about POPLoad.",
        intro="This is my last note on this. If matching receipts by hand is not a pain for {name}, no problem at all.",
        list_title="Two options",
        items=["Yes: reply YES and I will send the early-access install link.",
               "No: ignore this and I will not email you again."],
        closing="Either way, I wish {name} a great season.",
        cta="Reply YES", cta_subject="POPLoad early access"),
}


# ---------- importing ----------
def domain_of(url):
    url = str(url or "").strip()
    if not re.match(r"https?://", url, re.I):
        url = "https://" + url
    host = (urlparse(url).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host) else ""


HEADER_MAP = {"merchant brand": "name", "name": "name", "business": "name", "brand": "name",
              "niche / products": "niche", "niche": "niche", "products": "niche",
              "platform / domain": "website", "website": "website", "domain": "website", "url": "website",
              "manual payment instructions": "claim", "claim": "claim", "notes": "claim", "email": "email"}


def parse_csv(text):
    rows = [r for r in csv.reader(io.StringIO(text or "")) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [HEADER_MAP.get(h.strip().lower()) for h in rows[0]]
    if "website" in head:
        return [{k: v for k, v in zip(head, r) if k} for r in rows[1:]]
    return [dict(zip(["name", "niche", "website", "claim"], r)) for r in rows]


def add_rows(con, rows):
    """Returns (results, truncated); results is a list of (label, 'added'|'skipped', note)."""
    results = []
    for r in rows[:MAX_ROWS]:
        name = " ".join(str(r.get("name") or "").split())[:80]
        dom = domain_of(r.get("website"))
        if not dom or not name:
            results.append((name or r.get("website", ""), "skipped", "needs a business name and a website"))
            continue
        if con.execute("SELECT 1 FROM prospects WHERE domain=?", (dom,)).fetchone():
            results.append((name, "skipped", "already in the list"))
            continue
        if any(domain_of(l["website"]) == dom for l in con.execute("SELECT website FROM leads WHERE website!=''")):
            results.append((name, "skipped", "already a store-build lead"))
            continue
        now = db.now()
        con.execute("INSERT INTO prospects (name,website,domain,niche,claim,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (name, "https://" + dom, dom, str(r.get("niche") or "")[:120], str(r.get("claim") or "")[:300], now, now))
        results.append((name, "added", ""))
    con.commit()
    return results, len(rows) > MAX_ROWS


# ---------- finding more ----------
# Stores that take manual payments say so on their own pages. These phrases find those pages through a search
# API (no scraping). Results are only candidates: verification below decides whether they are real.
DISCOVERY_QUERIES = [
    '"proof of payment" GCash "order number" {niche} Philippines',
    '"send screenshot" "bank transfer" GCash {niche} Philippines shop',
    'myshopify.com {niche} Philippines GCash bank transfer',
    '"message us on Messenger" order {niche} Philippines Shopify',
]
SKIP_HOSTS = config.NON_STORE_HOSTS + ["google.", "bing.", "wikipedia.", "reddit.", "pinterest.", "medium.com", "blogspot.",
                                              "wordpress.com", "shopify.com", "apps.shopify", "forbes.", "yahoo.", "gov.ph", "edu.ph"]
PER_QUERY = 10


def adopt_shopify_leads(con, log=print):
    """Store-build leads whose own site turned out to run Shopify don't need a store, but they are POPLoad
    prospects. Copy them across (verification then decides); the lead itself is left as it was."""
    rows = con.execute("SELECT * FROM leads WHERE shopify_status='has_shopify' AND website!='' AND status NOT IN ('merged','do_not_contact')").fetchall()
    cands = [{"name": r["name"] or domain_of(r["website"]), "website": r["website"], "niche": (r["snippet"] or "")[:120],
              "claim": ""} for r in rows
             if not con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (r["url"],)).fetchone()]
    added = 0
    for c in cands:
        dom = domain_of(c["website"])
        if dom and not con.execute("SELECT 1 FROM prospects WHERE domain=?", (dom,)).fetchone():
            # add_rows skips store-build leads by design; this is the one place that is wanted, so insert directly
            now = db.now()
            con.execute("INSERT INTO prospects (name,website,domain,niche,claim,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                        (" ".join(c["name"].split())[:80], "https://" + dom, dom, c["niche"], "from the lead list", now, now))
            added += 1
    con.commit()
    if added:
        log(f"adopted {added} Shopify store(s) from the lead list")
    return added


def discovery_queries(today=None, n=4):
    """Rotate through niche x phrase so each day's few searches cover something new."""
    ordinal = (today or date.today()).toordinal()
    out = []
    for i in range(n):
        k = ordinal * n + i
        out.append(DISCOVERY_QUERIES[k % len(DISCOVERY_QUERIES)].format(niche=config.NICHES[(k // len(DISCOVERY_QUERIES)) % len(config.NICHES)]))
    return out


def discover(con, search_fn, queries, log=print):
    """Run the queries and add each result's site as a prospect. Returns the number added."""
    added = 0
    for q in queries:
        try:
            results = search_fn(q)
        except Exception as e:  # a bad query or rate limit must not stop the rest
            log(f"! {q}: {e}")
            continue
        rows, seen = [], set()
        for r in results:
            dom = domain_of(r.get("url"))
            if not dom or dom in seen or any(h in dom for h in SKIP_HOSTS):
                continue
            seen.add(dom)
            rows.append({"name": search.clean_name(r.get("title", "")).split(" - ")[0].split(" | ")[0][:80] or dom,
                         "niche": (r.get("snippet") or "")[:120], "website": dom, "claim": "found by search: " + q[:60]})
        results_, _ = add_rows(con, rows[:PER_QUERY])
        n = sum(1 for _, s, _ in results_ if s == "added")
        added += n
        log(f"{n:>3} new <- {q}")
    return added


# ---------- verifying ----------
def plausible_email(e):
    local, _, domain = e.lower().partition("@")
    if not local or not domain or any(p in e.lower() for p in PLACEHOLDER) or domain.rsplit(".", 1)[-1] not in TLDS:
        return False
    return not re.search(r"\.(ph|com|net|org)(?=[a-z]{3,})", domain)  # run-together text like ".phcontact"


def pick_emails(emails, domain):
    """Best first: an address on the shop's own domain, then a role mailbox, then the rest."""
    base = ".".join(domain.split(".")[-2:]) if not domain.endswith((".com.ph", ".net.ph", ".org.ph")) else ".".join(domain.split(".")[-3:])
    def rank(e):
        local, _, d = e.partition("@")
        return (0 if d == domain or d.endswith("." + base) or d == base else 1, 0 if local.startswith(ROLE_LOCALS) else 1)
    return sorted((e for e in emails if plausible_email(e)), key=rank)


def assess_payments(text):
    plain = re.sub(r"<[^>]+>", " ", text)
    seen = [label for label, rx in METHODS if re.search(rx, plain, re.I)]
    level = "proof" if PROOF_RE.search(plain) and seen else "mention" if seen else "none"
    return level, seen


def verify_site(prospect, fetch=shopify_check.fetch):
    """Look at the shop's own pages. Returns a dict of what was found; never raises."""
    out = {"platform": "unreachable", "pay_level": "", "pay_methods": [], "emails": [], "email_source": ""}
    pages = {}
    for path in PAGES:
        try:
            pages[path] = fetch(urljoin(prospect["website"] + "/", path.lstrip("/")))
        except (urllib.error.URLError, OSError, ValueError):
            if path == "/":
                return out
    home, headers = pages["/"]
    out["platform"] = shopify_check.classify_html(home, headers)
    blob = " ".join(h for h, _ in pages.values())
    out["pay_level"], out["pay_methods"] = assess_payments(blob)
    found = pick_emails(emailing.extract_emails(blob), prospect["domain"])
    out["emails"] = found[:4]
    for path, (h, _) in pages.items():
        if found and found[0] in h.lower():
            out["email_source"] = prospect["website"] + path
            break
    return out


def apply_verification(con, pid, res):
    p = con.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if res["platform"] == "unreachable":
        status, why = "rejected", "the website did not load (or the domain does not exist)"
    elif res["platform"] != "has_shopify":
        status, why = "rejected", "not a Shopify store, so POPLoad can't be installed"
    elif res["pay_level"] == "none":
        status, why = "rejected", "no sign of GCash, Maya or bank-transfer payments on the site"
    elif not res["emails"]:
        status, why = "needs_email", "no business email is published on the site; add one you found yourself"
    else:
        status, why = "verified", ""
    email = res["emails"][0] if res["emails"] else None
    if email and emailing.is_suppressed(con, email):
        status, why = "rejected", "that address opted out or bounced"
    if p["status"] in ("approved", "replied", "won", "lost", "done"):
        return  # never change a prospect that is already in the sequence
    con.execute("UPDATE prospects SET platform=?, pay_level=?, pay_methods=?, email=COALESCE(?,email), email_source=COALESCE(?,email_source), "
                "email_alts=?, status=?, reject_reason=?, verified_at=?, updated_at=? WHERE id=?",
                (res["platform"], res["pay_level"], ", ".join(res["pay_methods"]), email, res["email_source"] or None,
                 ", ".join(res["emails"][1:]), status, why, db.now(), db.now(), pid))
    con.commit()


def verify_all(con, fetch=shopify_check.fetch, limit=60, workers=8, log=print):
    rows = con.execute("SELECT * FROM prospects WHERE status='new' ORDER BY id LIMIT ?", (limit,)).fetchall()
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(lambda p: verify_site(p, fetch), rows))
    for p, res in zip(rows, results):  # database writes stay on one thread
        apply_verification(con, p["id"], res)
    log(f"verified {len(rows)} prospect(s)")
    return len(rows)


# ---------- actions ----------
def set_email(con, pid, text):
    found = emailing.extract_emails(text)
    if not found or not plausible_email(found[0]):
        return "That is not a usable business email."
    if emailing.is_suppressed(con, found[0]):
        return "That address opted out or bounced."
    con.execute("UPDATE prospects SET email=?, email_source='added manually', updated_at=?, "
                "status=CASE WHEN status='needs_email' THEN 'verified' ELSE status END WHERE id=?", (found[0], db.now(), pid))
    con.commit()
    return f"Email set to {found[0]}."


def apply_action(con, pid, action, value=""):
    p = con.execute("SELECT * FROM prospects WHERE id=?", (pid,)).fetchone()
    if not p:
        return "Unknown prospect."
    if action == "email":
        return set_email(con, pid, value)
    if action == "approve":
        if p["status"] != "verified" or not p["email"]:
            return "Only verified prospects with an email can be approved."
        if emailing.is_suppressed(con, p["email"]):
            return "That address opted out or bounced."
        if con.execute("SELECT 1 FROM prospects WHERE lower(email)=? AND status IN ('approved','replied','won','done') AND id!=?", (p["email"].lower(), pid)).fetchone():
            return "Another prospect with this email is already in the sequence."
        if con.execute("SELECT 1 FROM emails WHERE lower(to_email)=? AND status!='failed'", (p["email"].lower(),)).fetchone():
            return "That address was already emailed through the store-build track."
        for step, _ in STEPS:
            con.execute("INSERT OR IGNORE INTO prospect_steps (prospect_id,step) VALUES (?,?)", (pid, step))
        con.execute("UPDATE prospects SET status='approved', approved_at=?, updated_at=? WHERE id=?", (db.now(), db.now(), pid))
        msg = "Approved. The three-step sequence starts at the next send window."
    elif action == "reject":
        con.execute("UPDATE prospects SET status='rejected', reject_reason='rejected by you', updated_at=? WHERE id=?", (db.now(), pid))
        msg = "Rejected."
    elif action == "reconsider" and p["status"] == "rejected":
        con.execute("UPDATE prospects SET status=CASE WHEN email IS NULL OR email='' THEN 'needs_email' ELSE 'verified' END, reject_reason=NULL, updated_at=? WHERE id=?", (db.now(), pid))
        msg = "Moved back for review."
    elif action in ("replied", "won", "lost", "dnc") and p["status"] != "new":
        if action == "dnc":
            if p["email"]:
                emailing.suppress(con, p["email"], "marked do not contact")
            action = "done"
        con.execute("UPDATE prospects SET status=?, updated_at=? WHERE id=?", (action if action != "lost" else "lost", db.now(), pid))
        con.execute("UPDATE prospect_steps SET status='skipped', note=? WHERE prospect_id=? AND status='pending'", (f"marked {action}", pid))
        msg = "Updated; the rest of the sequence is cancelled."
    else:
        return "Nothing to do."
    con.commit()
    return msg


# ---------- the message ----------
def human_methods(p):
    m = [x for x in (p["pay_methods"] or "").split(", ") if x]
    return " and ".join([", ".join(m[:-1]), m[-1]] if len(m) > 2 else m) if m else "manual payments like GCash or bank transfer"


def build_message(prospect, step, base_url):
    c = COPY[step]
    name = " ".join(prospect["name"].split())[:60]
    fmt = dict(name=name, methods=human_methods(prospect))
    company = config.env("SENDER_COMPANY", "MindLab Future AI")
    sender = config.env("SENDER_NAME", "Mark")
    address = config.env("SENDER_ADDRESS", "Corporate Tower 2, BGC, Taguig City, Philippines")
    reply = config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com")
    unsub = f"{base_url}/unsubscribe?t={emailing.unsub_token(prospect['email'])}"
    why = f"You're getting this because {prospect['domain']} lists this address as a business contact."
    greeting, intro = COPY["greeting"].format(name=name), c["intro"].format(**fmt)
    subject, closing = c["subject"].format(**fmt), c["closing"].format(**fmt)
    numbered = "\n".join(f"{n}. {i}" for n, i in enumerate(c["items"], 1))
    text = (f"{greeting}\n\n{intro}\n\n{c['list_title']}:\n{numbered}\n\n{closing}\n\n{sender}\n{company}"
            f"\n\n--\n{why}\n{company}, {address}\nNot interested? Unsubscribe: {unsub} (or just reply STOP).")
    html = emailtemplate.render_followup(
        subject=subject, preheader=c["preheader"], greeting=greeting, intro=intro, list_title=c["list_title"],
        items=c["items"], closing=closing, cta_label=c["cta"],
        cta_mailto=f"mailto:{reply}?subject={quote(c['cta_subject'])}", signature=[sender, company],
        unsub_url=unsub, why=why, company=company, address=address, logo_url=config.env("LOGO_URL", emailtemplate.LOGO_URL))
    return {"subject": subject, "text": text, "html": html,
            "headers": {"List-Unsubscribe": f"<{unsub}>, <mailto:{reply}?subject=unsubscribe>",
                        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}}


# ---------- sending ----------
def sent_today(con, now=None):
    start = emailing.pht_day_start(now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return con.execute("SELECT COUNT(*) FROM prospect_steps WHERE status='sent' AND sent_at>=?", (start,)).fetchone()[0]


def _utc(ts):
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def due_steps(con, prospect, now):
    """The steps of this prospect that may go out now (at most one): the earliest pending step whose day has come."""
    steps = {r["step"]: r for r in con.execute("SELECT * FROM prospect_steps WHERE prospect_id=?", (prospect["id"],))}
    first = steps.get("intro")
    for step, days in STEPS:
        row = steps.get(step)
        if not row or row["status"] != "pending":
            continue
        if step == "intro":
            return row, 0
        if not first or first["status"] != "sent":
            return None, 0  # the sequence has not started
        late = (now - _utc(first["sent_at"])).total_seconds() / 86400 - days
        if late < 0:
            return None, 0
        return row, late
    return None, 0


def run(con, post=None, now=None, force=False, enabled=None, base_url=None, log=print, sleep=None):
    """Send every due step of every approved prospect. Returns the number sent. Dry run unless sending is enabled."""
    now = now or datetime.now(timezone.utc)
    if not force and not emailing.in_send_window(now):
        return 0
    enabled = config.env("EMAIL_SENDING_ENABLED").lower() == "true" if enabled is None else enabled
    base_url = base_url or config.base_url()
    if not base_url:
        log("popload sequence: BASE_URL not set (needed for the unsubscribe link)")
        return 0
    room = config.env_int("PROSPECT_DAILY_CAP", 10) - sent_today(con, now)
    sent = 0
    for p in con.execute("SELECT * FROM prospects WHERE status='approved' ORDER BY id").fetchall():
        if room <= 0:
            break
        if emailing.is_suppressed(con, p["email"]):
            con.execute("UPDATE prospects SET status='done', updated_at=? WHERE id=?", (db.now(), p["id"]))
            con.execute("UPDATE prospect_steps SET status='skipped', note='address opted out' WHERE prospect_id=? AND status='pending'", (p["id"],))
            con.commit()
            continue
        row, late = due_steps(con, p, now)
        if not row:
            continue
        if late > MAX_LATE_DAYS:
            con.execute("UPDATE prospect_steps SET status='skipped', note='too late to send' WHERE id=?", (row["id"],))
            con.commit()
            continue
        msg = build_message(p, row["step"], base_url)
        if not enabled:
            log(f"DRY RUN popload '{row['step']}' -> {p['email']}: {msg['subject']}")
            continue
        sender = config.env("SENDER_NAME", "Mark")
        payload = {"from": f"{sender} <{config.env('SENDER_FROM_EMAIL')}>", "to": [p["email"]],
                   "reply_to": config.env("SENDER_EMAIL", "support@mindlabfuture-ai.com"),
                   "subject": msg["subject"], "text": msg["text"], "html": msg["html"], "headers": msg["headers"]}
        send = post or (lambda pl, k: emailing.resend_post(pl, config.env("RESEND_API_KEY"), k))
        try:
            res = send(payload, f"prospect-{p['id']}-{row['step']}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            attempts = row["attempts"] + 1
            con.execute("UPDATE prospect_steps SET attempts=?, status=?, note=? WHERE id=?",
                        (attempts, "failed" if attempts >= MAX_ATTEMPTS else "pending", str(e)[:200], row["id"]))
            con.commit()
            log(f"popload send failed ({p['email']}, {row['step']}): {e}")
            continue
        con.execute("UPDATE prospect_steps SET status='sent', sent_at=?, resend_id=? WHERE id=?", (now.isoformat(timespec="seconds"), res.get("id"), row["id"]))
        con.commit()
        sent += 1
        room -= 1
        log(f"sent popload '{row['step']}' to {p['email']}")
        if sleep:
            sleep(60)
    return sent
