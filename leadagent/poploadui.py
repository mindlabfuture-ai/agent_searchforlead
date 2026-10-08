"""The Prospects page: import a list, see what verification found, approve who gets the POPLoad sequence."""
import html

from . import config, popload

E = lambda v: html.escape(str(v if v is not None else ""), quote=True)  # everything here came from the web

STYLE = ("<style>body{font-family:system-ui;max-width:900px;margin:1rem auto;padding:0 16px}"
         "section{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}"
         "label{display:block;margin:8px 0 2px}input[type=text],textarea{width:100%;box-sizing:border-box;padding:6px}"
         ".b{padding:8px;border-radius:6px}button{margin:2px;padding:6px 12px}.ok{color:#060}.no{color:#a00}.tag{font-size:12px;padding:2px 8px;border-radius:99px;background:#eee}"
         "nav a{margin-right:12px}</style>")
FILTERS = [("verified", "Ready to approve"), ("needs_email", "Needs an email"), ("approved", "In the sequence"),
           ("rejected", "Rejected"), ("new", "Not checked yet"), ("all", "All")]
PAIN_NAMES = {"email": "by email", "messenger": "via Messenger/chat", "order_no": "order number needed", "before_dispatch": "checked before dispatch", "deadline": "payment deadline"}


def PAIN_LABEL(pain):
    return ", ".join(PAIN_NAMES.get(k, k) for k in (pain or "").split(",") if k) or "nothing specific seen"


STEP_MARK = {"sent": "sent", "pending": "waiting", "skipped": "skipped", "failed": "FAILED"}


def _btn(tok, pid, action, label, extra=""):
    return (f"<form method=post action=/prospects style=display:inline>{tok}<input type=hidden name=mode value=action>"
            f"<input type=hidden name=id value={pid}><button name=action value={action}>{label}</button>{extra}</form>")


def render_page(con, tok, message="", show="verified", results=None):
    tok = f"<input type=hidden name=csrf value={tok}>"
    sending = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    counts = dict(con.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status").fetchall())
    nav = " ".join(f"<a href='/prospects?show={k}'>{E(label)} ({sum(counts.values()) if k == 'all' else counts.get(k, 0)})</a>" for k, label in FILTERS)
    q = "SELECT * FROM prospects" + ("" if show == "all" else " WHERE status=?") + \
        " ORDER BY pain_score DESC, CASE pay_level WHEN 'proof' THEN 0 WHEN 'mention' THEN 1 ELSE 2 END, id LIMIT 200"
    rows = con.execute(q, (() if show == "all" else (show if show in dict(FILTERS) else "verified",))).fetchall()
    cards = []
    for p in rows:
        steps = con.execute("SELECT * FROM prospect_steps WHERE prospect_id=? ORDER BY id", (p["id"],)).fetchall()
        line = " &middot; ".join(f"{E(s['step'])}: {STEP_MARK.get(s['status'], E(s['status']))}{' ' + E(s['sent_at'][:10]) if s['sent_at'] else ''}" for s in steps)
        email = (f"{E(p['email'])} <small>(from {E(p['email_source'])})</small>" if p["email"] else "<span class=no>no email yet</span>")
        alts = f"<br><small>other addresses seen: {E(p['email_alts'])}</small>" if p["email_alts"] else ""
        why = f"<br><span class=no>{E(p['reject_reason'])}</span>" if p["reject_reason"] else ""
        claim = f"<br><small>Your list says: {E(p['claim'])}</small>" if p["claim"] else ""
        pay = {"proof": "asks buyers for proof of payment", "mention": "mentions manual payments", "none": "no manual payments seen"}.get(p["pay_level"], "")
        fit = f"<br>Fit: <strong>{popload.pain_class(p['pain_score'] or 0)}</strong> ({p['pain_score'] or 0}) &middot; proof: {E(PAIN_LABEL(p['pain']))}"
        b = ""
        if p["status"] == "verified":
            b = _btn(tok, p["id"], "approve", "Approve sequence") + _btn(tok, p["id"], "reject", "Reject")
        elif p["status"] == "needs_email":
            b = (f"<form method=post action=/prospects>{tok}<input type=hidden name=mode value=action><input type=hidden name=id value={p['id']}>"
                 f"<input type=hidden name=action value=email><input type=text name=value placeholder='public business email you found'>"
                 f"<button>Save email</button></form>") + _btn(tok, p["id"], "reject", "Reject")
        elif p["status"] == "approved":
            b = _btn(tok, p["id"], "replied", "They replied") + _btn(tok, p["id"], "won", "Won") + _btn(tok, p["id"], "lost", "Lost") + _btn(tok, p["id"], "dnc", "Do not contact")
        elif p["status"] == "rejected":
            b = _btn(tok, p["id"], "reconsider", "Reconsider")
        cards.append(f"<section><h3>{E(p['name'])} <span class=tag>{E(p['status'])}</span></h3>"
                     f"<a href=\"{E(p['website'])}\" rel=\"noopener noreferrer\" target=_blank>{E(p['domain'])}</a> &middot; {E(p['niche'])}<br>"
                     f"Shopify: {E(p['platform'])} &middot; {E(pay)} {('(' + E(p['pay_methods']) + ')') if p['pay_methods'] else ''}{fit}<br>{email}{alts}{why}{claim}"
                     f"{('<p><small>' + line + '</small></p>') if line else ''}<p>{b}</p></section>")
    res = ""
    if results is not None:
        added = sum(1 for _, s, _ in results if s == "added")
        res = ("<section><h3>%d added, %d skipped</h3><ul>" % (added, len(results) - added) +
               "".join(f"<li class={'ok' if s == 'added' else 'no'}>{E(n)}: {E(s)}{' - ' + E(note) if note else ''}</li>" for n, s, note in results) +
               "</ul><p>Checking each site now. Refresh in a minute.</p></section>")
    banner = "LIVE: approved prospects are emailed Mon-Fri 9-17 PHT" if sending else "DRY RUN: nothing is sent until EMAIL_SENDING_ENABLED=true"
    msg = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>POPLoad prospects</title>{STYLE}"
            f"<nav><a href='/'>Lead queue</a><a href='/previews'>Previews</a><a href='/clients'>Clients</a></nav><h1>POPLoad prospects</h1>"
            f"<p class=b style='background:{'#fde' if sending else '#ffd'}'>{E(banner)}</p>"
            f"<p>Sent today: {popload.sent_today(con)}/{config.env_int('PROSPECT_DAILY_CAP', 10)}. Each approved prospect gets 4 emails: day 0, 3, 7 (the demo video, if POPLOAD_DEMO_URL is set) and 14, highest fit first. "
            f"A reply, unsubscribe or bounce stops the rest; mark replies yourself with <em>They replied</em>.</p>{msg}{res}"
            f"<section><h3>Import a list</h3><form method=post action=/prospects>{tok}<input type=hidden name=mode value=import>"
            f"<p>Paste a CSV with a header row (<code>Merchant Brand, Niche / Products, Platform / Domain, Manual Payment Instructions</code>), rows of name, niche, website, notes, or just a list of store links, one per line (for example from the Meta Ad Library; the name is read from the store's own site). Social, marketplace and link-in-bio pages are skipped: paste the store's own website. "
            f"Each site is checked: it must load, run on Shopify, show manual-payment signs and publish a business email.</p>"
            f"<textarea name=csv rows=6></textarea><p><button>Import and verify</button></p></form></section>"
            f"<p>{nav}</p>{''.join(cards) or '<p>Nothing here.</p>'}")
