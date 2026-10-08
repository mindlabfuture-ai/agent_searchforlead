"""POPLoad prospecting: existing Shopify stores that take GCash / bank transfer and could use receipt uploads.

A separate track from the store-build leads. Rows are imported from a list, then *verified* from the sites
themselves: the domain must load, the site must run on Shopify (POPLoad is a Shopify app), and a business email
must be published on the site's own pages. Nothing is guessed or taken from anywhere else. A person approves each
prospect before anything is sent; the four-touch sequence then runs on its own and stops on a reply, an
unsubscribe, a bounce or a complaint."""
import csv
import html as htmllib
import io
import re
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin, urlparse

from . import config, db, emailing, emailtemplate, search, shopify_check

MAX_ROWS = 300
PAGES = ["/", "/pages/contact", "/pages/contact-us", "/pages/payment", "/pages/payment-options", "/pages/payment-methods", "/pages/how-to-order",
         "/pages/how-to-pay", "/pages/faq", "/policies/refund-policy", "/policies/shipping-policy"]
TLDS = {"com", "ph", "net", "org", "co", "io", "biz", "shop", "store", "asia", "info", "me", "ph", "online"}
PLACEHOLDER = ("xxx", "example", "yourdomain", "yourname", "domain", "email", "name@", "user@", "test@", "sample")
ROLE_LOCALS = ("hello", "hi", "info", "support", "sales", "contact", "shop", "orders", "care", "customer", "help", "admin")
PROOF_RE = re.compile(r"proof of payment|payment proof|deposit slip|payment (screenshot|receipt|confirmation)|transfer (receipt|confirmation|proof)", re.I)
METHODS = [("GCash", r"\bg-?cash\b"), ("Maya", r"\b(pay)?maya\b"), ("bank transfer", r"\bbank (transfer|deposit)\b|\bbdo\b|\bbpi\b|\bunionbank\b|\bmetrobank\b")]

# step, days after the first email
STEPS = [("intro", 0), ("reminder", 3), ("demo", 7), ("last_note", 14)]
MAX_LATE_DAYS = 7
HOT, WARM, POTENTIAL = 12, 8, 5  # pain-score cut-offs
MAX_ATTEMPTS = 3

COPY = {
    "greeting": "Hi {name} team,",
    "intro": dict(
        subject="Chasing payment screenshots at {name}?",
        preheader="Customers upload the receipt with the order; you approve it in one click.",
        intro="{observation} {pain} I built POPLoad, a Shopify app, so customers upload the receipt straight to their order instead.",
        list_title="What POPLoad does",
        items=["Upload: customers pay by bank transfer, GCash or Maya, then upload the receipt right on the order page.",
               "Approve: you see it in your Shopify admin, attached to the order, and approve it with one click. The order is marked as paid.",
               "Calm inbox: no more digging through emails and chats for proof of payment."],
        closing="POPLoad is still in Shopify's app review, so I am onboarding a small number of Philippine stores personally, and the first 10 receipt uploads are free. Want to see how it works? I can send a 2-minute demo, and you can try it yourself on my demo store.",
        cta="Yes, send the demo", cta_subject="POPLoad demo"),
    "reminder": dict(
        subject="Quick question about payment proofs",
        preheader="Would a 2-minute demo be useful?",
        intro="Just following up. I noticed {name} accepts {methods} and collects payment proof {where}. POPLoad was built to put that receipt directly with the Shopify order.",
        list_title="The flow",
        items=["The customer pays from their own app and uploads the receipt on the order page.",
               "You open the order in Shopify and the receipt is already there.",
               "You approve it, and the order shows as paid."],
        closing="Would it be useful if I sent you a 2-minute demo?",
        cta="Yes, send the demo", cta_subject="POPLoad demo"),
    "demo": dict(
        subject="A 2-minute POPLoad example",
        preheader="Customer pays, uploads the receipt, you see it with the order.",
        intro="I made a 2-minute example of the workflow: the customer pays, uploads the receipt, and you see it with the order in Shopify. Here it is.",
        list_title="What you will see",
        items=["Before: a screenshot arrives in Messenger or email and someone has to find the order.",
               "After: the receipt is already attached to the order.",
               "Your side: review it and approve, in one place."],
        closing="If {name} collects payment proof by hand today, this is the step POPLoad takes away. I am happy to walk you through it.",
        cta="Watch the 2-minute demo", cta_subject="POPLoad demo"),
    "last_note": dict(
        subject="Should I close the loop?",
        preheader="My last note about POPLoad.",
        intro="I reached out because {name}'s payment-proof process looks like the workflow POPLoad was built to simplify. If it is not a pain right now, no problem at all.",
        list_title="Two options",
        items=["Yes: reply YES and I will send the 2-minute demo.",
               "No: ignore this and I will not email you again."],
        closing="Either way, I wish {name} a great season.",
        cta="Reply YES", cta_subject="POPLoad demo"),
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


def _delimiter(text):
    """Pasted tables arrive comma-separated from a CSV file but tab-separated from a spreadsheet; use whichever the header row uses."""
    first = next((l for l in (text or "").lstrip("\ufeff").splitlines() if l.strip()), "")
    counts = {d: first.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def parse_csv(text):
    text = (text or "").lstrip("\ufeff")
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=_delimiter(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [HEADER_MAP.get(h.strip().lower()) for h in rows[0]]
    if "website" in head:
        return [{k: v for k, v in zip(head, r) if k} for r in rows[1:]]
    return [row for r in rows for row in _headerless(r)]


def _is_link(cell):
    cell = cell.strip()
    return bool(cell) and not re.search(r"\s", cell) and bool(domain_of(cell)) and "." in cell


def _headerless(cells):
    """A row without a header: find the cell that is a web address; the others are name, niche and notes in that order.
    Several addresses on one row (a pasted list) become one row each, so a plain list of store links works."""
    cells = [c.strip() for c in cells]
    if len(cells) == 1 and len(cells[0].split()) > 1 and all(_is_link(t) for t in cells[0].split()):
        cells = cells[0].split()  # space-separated addresses
    links = [c for c in cells if _is_link(c)]
    if not links:
        return [dict(zip(["name", "niche", "website", "claim"], cells))]
    if len(links) > 1:
        return [{"website": l} for l in links]
    others = [c for c in cells if c and c != links[0]]
    return [dict(zip(["name", "niche", "claim"], others), website=links[0])]


def name_from_domain(dom):
    """A readable placeholder; verification replaces it with the name the shop uses on its own site."""
    parts = dom.split(".")
    while len(parts) > 1 and parts[-1] in ("com", "net", "org", "co", "ph", "asia", "shop", "store", "online", "io"):
        parts.pop()
    label = parts[-1]  # shop.brand.com.ph -> brand
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", label) if w) or dom


def add_rows(con, rows):
    """Returns (results, truncated); results is a list of (label, 'added'|'skipped', note)."""
    results = []
    for r in rows[:MAX_ROWS]:
        name = " ".join(str(r.get("name") or "").split())[:80]
        dom = domain_of(r.get("website"))
        if not dom:
            results.append((name or str(r.get("website", ""))[:80], "skipped", "needs a website"))
            continue
        if any(h.strip(".") in dom for h in config.NON_STORE_HOSTS):
            results.append((name or dom, "skipped", "a social, marketplace or link-in-bio page; paste the store's own website instead"))
            continue
        auto = 0 if name else 1
        name = name or name_from_domain(dom)
        if con.execute("SELECT 1 FROM prospects WHERE domain=?", (dom,)).fetchone():
            results.append((name, "skipped", "already in the list"))
            continue
        if any(domain_of(l["website"]) == dom for l in con.execute("SELECT website FROM leads WHERE website!=''")):
            results.append((name, "skipped", "already a store-build lead"))
            continue
        now = db.now()
        con.execute("INSERT INTO prospects (name,website,domain,niche,claim,name_auto,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (name, "https://" + dom, dom, str(r.get("niche") or "")[:120], str(r.get("claim") or "")[:300], auto, now, now))
        results.append((name, "added", "name taken from the site when it is checked" if auto else ""))
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
            results = search.search_once(search_fn, q)
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


LINK_RE = re.compile(r"""href=["']([^"'#]+)""", re.I)
LINK_WORDS = re.compile(r"payment|how-to|order|faq|shipping|delivery|terms|polic|contact|checkout-info|bank|gcash", re.I)
EXTRA_PAGES = 8


def linked_pages(home, base):
    """Same-site pages the shop itself links to that are likely to explain payment (not guessed paths)."""
    host, out = urlparse(base).netloc.lower().removeprefix("www."), []
    for href in LINK_RE.findall(home or ""):
        u = urljoin(base + "/", href.strip())
        pu = urlparse(u)
        path = pu.path.rstrip("/") or "/"
        if pu.scheme not in ("http", "https") or pu.netloc.lower().removeprefix("www.") != host or path == "/":
            continue
        if re.search(r"/(products|collections|cart|account|blogs/[^/]+/.+|cdn)(/|$)|\.(jpg|png|webp|css|js|pdf)$", path, re.I) or not LINK_WORDS.search(path):
            continue
        key = pu.scheme + "://" + pu.netloc + path
        if key not in out:
            out.append(key)
    return out[:EXTRA_PAGES]


def site_name(home):
    """The shop's own name: og:site_name, else the front of the page title (before ' - ', ' | ' or an en dash)."""
    m = (re.search(r"""<meta[^>]+property=["']og:site_name["'][^>]+content=["']([^"']+)""", home or "", re.I)
         or re.search(r"""<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:site_name["']""", home or "", re.I)
         or re.search(r"<title[^>]*>(.*?)</title>", home or "", re.I | re.S))
    name = " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split()) if m else ""
    name = re.split(r"\s+[-|\u2013\u2014:]\s+", name)[0].strip(" -|")
    return name if 2 <= len(name) <= 60 and not re.search(r"https?://|^home$|^welcome", name, re.I) else ""


def verify_site(prospect, fetch=shopify_check.fetch):
    """Look at the shop's own pages. Returns a dict of what was found; never raises."""
    out = {"platform": "unreachable", "pay_level": "", "pay_methods": [], "pain": [], "emails": [], "email_source": "", "site_name": ""}
    base, pages = prospect["website"], {}
    for attempt in range(2):  # one retry: a shop that is merely slow should not be rejected as missing
        try:
            pages["/"] = fetch(base + "/")
            break
        except (urllib.error.URLError, OSError, ValueError):
            if attempt:
                return out
    for path in PAGES[1:]:
        try:
            pages[path] = fetch(urljoin(base + "/", path.lstrip("/")))
        except (urllib.error.URLError, OSError, ValueError):
            pass
    for url in linked_pages(pages["/"][0], base):
        if url not in pages:
            try:
                pages[url] = fetch(url)
            except (urllib.error.URLError, OSError, ValueError):
                pass
    home, headers = pages["/"]
    out["platform"] = shopify_check.classify_html(home, headers)
    out["site_name"] = site_name(home)
    blob = " ".join(h for h, _ in pages.values())
    out["pay_level"], out["pay_methods"] = assess_payments(blob)
    out["pain"] = detect_pain(blob)
    found = pick_emails(emailing.extract_emails(blob), prospect["domain"])
    out["emails"] = found[:4]
    for path, (h, _) in pages.items():
        if found and found[0] in h.lower():
            out["email_source"] = path if path.startswith("http") else base + path
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
    if res.get("site_name") and p["name_auto"]:
        con.execute("UPDATE prospects SET name=?, name_auto=0 WHERE id=?", (res["site_name"], pid))
    con.execute("UPDATE prospects SET platform=?, pay_level=?, pay_methods=?, pain=?, pain_score=?, email=COALESCE(?,email), email_source=COALESCE(?,email_source), "
                "email_alts=?, status=?, reject_reason=?, verified_at=?, updated_at=? WHERE id=?",
                (res["platform"], res["pay_level"], ", ".join(res["pay_methods"]), ",".join(res["pain"]),
                 pain_score(res["pain"], res["platform"], res["pay_level"]), email, res["email_source"] or None,
                 ", ".join(res["emails"][1:]), status, why, db.now(), db.now(), pid))
    con.commit()


# What the shop's own pages say about how proof of payment reaches them. Only wording found on the site counts.
_PROOF = r"(proof of (payment|transfer|deposit)|payment proof|deposit slip|payment slip|(payment|deposit|transfer|bank|gcash|maya|transaction) (receipt|screenshot|slip)|screenshot of (the |your |a )?(payment|transaction|deposit|transfer|receipt|bank|gcash)|receipt of (payment|deposit|transfer))"  # not a bare "receipt": that is also the delivery of an item
_EMAIL = r"(e-?mail|@[\w-]+)"
_CHAT = r"((via|through|thru|using|on|to|by|in|or|and|/)\s+(our\s+|the\s+)?(official\s+)?(facebook|fb|instagram|ig|messenger|viber|whatsapp)\b|messenger|viber|whatsapp|direct message|\bdm\b|\bpm us\b)"  # a social link in a menu ("Facebook Instagram") is not a way to send proof
PAIN_RULES = {
    "email": (rf"{_EMAIL}.{{0,90}}{_PROOF}|{_PROOF}.{{0,90}}{_EMAIL}", 3),
    "messenger": (rf"{_CHAT}.{{0,100}}{_PROOF}|{_PROOF}.{{0,100}}{_CHAT}", 3),
    "order_no": (rf"order (number|no\b|#|id)[^.]{{0,100}}{_PROOF}|{_PROOF}[^.]{{0,100}}order (number|no\b|#|id)", 2),
    "before_dispatch": (rf"{_PROOF}[^.]{{0,100}}(before|prior to|until)[^.]{{0,40}}(ship|dispatch|process|pack|deliver)|(before|prior to)[^.]{{0,40}}(ship|dispatch|process|pack)[^.]{{0,80}}{_PROOF}|(ship|dispatch|process|pack)[^.]{{0,40}}(after|once|when)[^.]{{0,40}}(payment|{_PROOF})[^.]{{0,30}}(verif|confirm|cleared)", 2),
    "deadline": (rf"(within|in) (\d+|one|two|a) ?(hours?|hrs?|days?)[^.]{{0,100}}{_PROOF}|{_PROOF}[^.]{{0,100}}(within|in) (\d+|one|two|a) ?(hours?|hrs?|days?)|(auto-?cancel|will be cancel)", 2),
}


def detect_pain(text):
    """The proof-of-payment habits visible in the shop's own text, as a list of keys from PAIN_RULES."""
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", text)))
    plain = re.sub(r"(?<=\w)\.(?=\w)", "_", __import__("html").unescape(plain))  # dots inside addresses are not sentence ends
    return [k for k, (rx, _) in PAIN_RULES.items() if re.search(rx, plain, re.I)]


def pain_score(pain, platform, pay_level):
    """Fit score: +3 proof by email, +3 by Messenger/chat, +3 manual payments taken, +2 each for order number,
    verification before dispatch and a deadline, +2 Shopify confirmed."""
    return (sum(PAIN_RULES[k][1] for k in pain if k in PAIN_RULES) + (3 if pay_level in ("proof", "mention") else 0)
            + (2 if platform == "has_shopify" else 0))


def pain_class(score):
    return "Hot" if score >= HOT else "Warm" if score >= WARM else "Potential" if score >= POTENTIAL else "Low"


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
        msg = "Approved. The four-email sequence (days 0, 3, 7 and 14) starts at the next send window."
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


def _pain(p):
    return [k for k in ((p["pain"] if "pain" in p.keys() else "") or "").split(",") if k]


def where_proof(p):
    pain = _pain(p)
    if "email" in pain and "messenger" in pain:
        return "by email or social media message"
    return "by email" if "email" in pain else "through social media messages" if "messenger" in pain else "by hand"


def observation(p, name):
    """The opening line. It states only what the shop's own pages showed; with nothing specific it stays general."""
    pain, methods = _pain(p), human_methods(p)
    if "email" in pain and "messenger" in pain:
        return f"I noticed {name} asks customers to send their payment proof by email or social media message after they order."
    if "email" in pain:
        return f"I noticed {name} asks customers to email their payment proof after they place an order."
    if "messenger" in pain:
        return f"I noticed {name} takes {methods} and customers send their payment proof through social media messages."
    if (p["pay_level"] if "pay_level" in p.keys() else "") == "proof":
        return f"I noticed {name} takes {methods} and asks customers for proof of payment."
    return f"I noticed {name} takes {methods}, so you probably get payment receipts by email, Messenger or Viber and match them to orders by hand."


def pain_sentence(p):
    base = "The catch is that the receipt arrives separately from the Shopify order, so someone has to work out which order it belongs to."
    if "messenger" in _pain(p):
        base += " Chat is great for talking to customers, but it is not the best place to keep payment receipts."
    return base


def build_message(prospect, step, base_url):
    c = COPY[step]
    name = " ".join(prospect["name"].split())[:60]
    fmt = dict(name=name, methods=human_methods(prospect), where=where_proof(prospect),
               observation=observation(prospect, name), pain=pain_sentence(prospect))
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
    demo = config.env("POPLOAD_DEMO_URL")
    button = demo if step == "demo" and demo else f"mailto:{reply}?subject={quote(c['cta_subject'])}"
    if step == "demo" and demo:
        text = text.replace(f"{closing}\n\n", f"{closing}\n\nWatch it here: {demo}\n\n", 1)
    html = emailtemplate.render_followup(
        subject=subject, preheader=c["preheader"], greeting=greeting, intro=intro, list_title=c["list_title"],
        items=c["items"], closing=closing, cta_label=c["cta"],
        cta_mailto=button, signature=[sender, company],
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
    for p in con.execute("SELECT * FROM prospects WHERE status='approved' ORDER BY pain_score DESC, id").fetchall():
        if room <= 0:
            break
        for step, _ in STEPS:  # prospects approved before a step existed still get it
            con.execute("INSERT OR IGNORE INTO prospect_steps (prospect_id,step) VALUES (?,?)", (p["id"], step))
        con.commit()
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
        if row["step"] == "demo" and not config.env("POPLOAD_DEMO_URL"):
            con.execute("UPDATE prospect_steps SET status='skipped', note='no POPLOAD_DEMO_URL set' WHERE id=?", (row["id"],))
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
