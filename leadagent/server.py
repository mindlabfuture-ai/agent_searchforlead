"""One small web service for Railway: approval dashboard, unsubscribe page, Resend webhook,
health check, and a background scheduler (daily pipeline + business-hours email sender)."""
import base64
import hashlib
import hmac
import html
import json
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, db, dedupe, emailing, importer, pipeline

E = lambda v: html.escape(str(v if v is not None else ""), quote=True)  # lead data comes from the web: always escape

PAGE_STYLE = ("<style>body{font-family:system-ui;max-width:860px;margin:1rem auto;padding:0 16px}"
              "section{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}pre{white-space:pre-wrap}"
              "label{display:block;margin:8px 0 2px}input[type=text],textarea{width:100%;box-sizing:border-box;padding:6px}"
              ".b{padding:8px;border-radius:6px}button{margin:2px;padding:6px 12px}.ok{color:#060}.no{color:#a00}</style>")


def csrf_token():
    return hmac.new(config.env("ADMIN_PASSWORD").encode(), b"csrf", hashlib.sha256).hexdigest()


def render_import_page(results=None, truncated=False):
    """Form to add leads by hand. `results` is importer.add_rows output, shown after a submit."""
    tok = f"<input type=hidden name=csrf value={csrf_token()}>"
    out = ""
    if results is not None:
        added = sum(1 for _, st, _ in results if st == "added")
        lines = "".join(f"<li class={'ok' if st == 'added' else 'no'}>{E(label or '(no name)')}: {E(st)}"
                        f"{' - ' + E(note) if note else ''}</li>" for label, st, note in results)
        more = f"<p class=no>Only the first {importer.MAX_ROWS} rows were read.</p>" if truncated else ""
        out = (f"<section><h3>{added} added, {len(results) - added} skipped</h3>{more}<ul>{lines}</ul>"
               f"<p>New leads are being checked and scored now. Give it a minute, then "
               f"<a href='/'>open the queue</a>.</p></section>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Import leads</title>{PAGE_STYLE}<p><a href='/'>&larr; Lead queue</a></p><h1>Import leads</h1>{out}"
            f"<section><h3>Add one lead</h3><form method=post action=/import>{tok}<input type=hidden name=mode value=single>"
            f"<label>Profile URL (Facebook page, Instagram, TikTok, Shopee, Lazada or Carousell)</label><input type=text name=url required>"
            f"<label>Business name</label><input type=text name=name>"
            f"<label>Notes (what they sell, where, anything useful)</label><input type=text name=snippet>"
            f"<label>Their website (if any)</label><input type=text name=website>"
            f"<label>Business email, only if they list it publicly</label><input type=text name=email>"
            f"<p><button>Add lead</button></p></form></section>"
            f"<section><h3>Paste a list (CSV)</h3><form method=post action=/import>{tok}<input type=hidden name=mode value=csv>"
            f"<p>One lead per line: <code>url,name,notes,website,email</code>. A header row is optional, "
            f"and only the URL is required. Up to {importer.MAX_ROWS} lines.</p>"
            f"<textarea name=csv rows=8 placeholder='https://facebook.com/glowph,Glow PH,skincare Manila,,'></textarea>"
            f"<p><button>Import list</button></p></form></section>"
            f"<p><small>Duplicates, opted-out businesses and addresses that bounced are skipped automatically. "
            f"Hand-added leads stay in the queue unless the business is already on Shopify.</small></p>")


def render_dashboard(con):
    sending = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    cap = config.env_int("EMAIL_DAILY_CAP", 20)
    banner = ("LIVE: approved emails are sent Mon-Fri 9-17 PHT" if sending
              else "DRY RUN: nothing is sent until EMAIL_SENDING_ENABLED=true")
    rows = con.execute("SELECT * FROM leads WHERE status IN ('qualified','drafted','approved') "
                       "ORDER BY (status='approved') DESC, score DESC LIMIT 100").fetchall()
    cards = []
    for r in rows:
        email_preview = ""
        if r["email"] and config.base_url() and len(config.env("UNSUB_SECRET")) >= 16:
            m = emailing.build_email(r, config.base_url())
            email_preview = f"<details><summary>Email preview</summary><b>{E(m['subject'])}</b><pre>{E(m['text'])}</pre></details>"
        btn = lambda action, label: (f"<form method=post action=/action style=display:inline>"
                                     f"<input type=hidden name=csrf value={csrf_token()}><input type=hidden name=id value={r['id']}>"
                                     f"<button name=action value={action}>{label}</button></form>")
        approve = btn("approve", "Approve email") if r["email"] and r["status"] != "approved" else ""
        cards.append(f"""<section><h3>{E(r['name'] or r['url'])} <small>{E(r['platform'])} &middot; score {r['score']} &middot; {E(r['status'])}</small></h3>
<a href="{E(r['url'])}" rel="noopener noreferrer" target=_blank>{E(r['url'])}</a>
<p>{E(r['score_notes'])}</p><p>Also on: {E(r['also_on'])}</p>
<p>Email: <b>{E(r['email'] or 'none')}</b> <small>{E(r['email_source'])}</small></p>
<form method=post action=/action><input type=hidden name=csrf value={csrf_token()}><input type=hidden name=id value={r['id']}>
<input name=email placeholder="business email (publicly listed)" size=32><button name=action value=set_email>Save email</button></form>
<details><summary>DM draft</summary><pre>{E(r['draft'])}</pre></details>{email_preview}
{approve}{btn('dm_sent', 'Mark DM sent')}{btn('skip', 'Skip')}{btn('dnc', 'Do not contact')}</section>""")
    stats = (f"Emails today: {emailing.sent_today(con)}/{cap} &middot; suppressed addresses: "
             f"{con.execute('SELECT COUNT(*) FROM suppressed_emails').fetchone()[0]}")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Lead queue</title><style>body{{font-family:system-ui;max-width:860px;margin:1rem auto;padding:0 16px}}"
            f"section{{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}}pre{{white-space:pre-wrap}}"
            f".b{{background:{'#fde' if sending else '#ffd'};padding:8px;border-radius:6px}}button{{margin:2px}}</style>"
            f"<h1>Lead queue</h1><p><a href='/import'>+ Import leads</a></p><p class=b>{E(banner)}</p><p>{stats}</p>{''.join(cards) or '<p>No leads waiting.</p>'}")


def apply_action(con, lead_id, action, email=""):
    r = con.execute("SELECT * FROM leads WHERE id=? AND status!='merged'", (lead_id,)).fetchone()
    if not r:
        return
    t = db.now()
    if action == "set_email":
        found = emailing.extract_emails(email)
        if found:
            con.execute("UPDATE leads SET email=?, email_source='added manually', updated_at=? WHERE id=?",
                        (found[0], t, lead_id))
    elif action == "approve" and r["email"] and not emailing.is_suppressed(con, r["email"]) \
            and r["status"] in ("qualified", "drafted"):
        con.execute("UPDATE leads SET status='approved', updated_at=? WHERE id=?", (t, lead_id))
    elif action == "dm_sent" and r["status"] in ("qualified", "drafted", "approved"):
        con.execute("UPDATE leads SET status='contacted', updated_at=? WHERE id=?", (t, lead_id))
    elif action == "skip":
        con.execute("UPDATE leads SET status='lost', updated_at=? WHERE id=?", (t, lead_id))
    elif action == "dnc":
        db.add_do_not_contact(con, r["url"], "marked in dashboard")
        if r["email"]:
            emailing.suppress(con, r["email"], "marked in dashboard")
    con.commit()


class Handler(BaseHTTPRequestHandler):
    server_version = "leadagent"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self):
        h = self.headers.get("Authorization", "")
        try:
            user, _, pw = base64.b64decode(h.split(" ", 1)[1]).decode().partition(":")
        except Exception:
            user = pw = ""
        ok = hmac.compare_digest(pw.encode(), config.env("ADMIN_PASSWORD").encode()) and user == "admin"
        if not ok:
            self._send(401, "Login required", headers=[("WWW-Authenticate", 'Basic realm="leads"')])
        return ok

    def _body(self):
        return self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1_000_000))

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, "ok", "text/plain")
        if u.path == "/unsubscribe":
            t = parse_qs(u.query).get("t", [""])[0]
            email = emailing.parse_unsub_token(t)
            if not email:
                return self._send(400, "Invalid unsubscribe link.")
            # GET only shows a button: mail scanners prefetch links and must not unsubscribe people.
            return self._send(200, f"<!doctype html><meta charset=utf-8><title>Unsubscribe</title>"
                                   f"<p>Stop emails to <b>{E(email)}</b>?</p><form method=post>"
                                   f"<button>Yes, unsubscribe</button></form>")
        if u.path == "/" and self._authed():
            con = db.connect(config.DB_PATH)
            return self._send(200, render_dashboard(con))
        if u.path == "/import" and self._authed():
            return self._send(200, render_import_page())
        if u.path != "/":
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        body = self._body()
        if u.path == "/unsubscribe":  # also the RFC 8058 one-click target
            email = emailing.parse_unsub_token(parse_qs(u.query).get("t", [""])[0])
            if not email:
                return self._send(400, "Invalid unsubscribe link.")
            emailing.suppress(db.connect(config.DB_PATH), email, "unsubscribed")
            return self._send(200, "You're unsubscribed. We won't email you again.")
        if u.path == "/webhooks/resend":
            if not emailing.verify_svix(config.env("RESEND_WEBHOOK_SECRET"), dict(self.headers), body):
                return self._send(401, "bad signature", "text/plain")
            result = emailing.handle_resend_event(db.connect(config.DB_PATH), json.loads(body or b"{}"))
            return self._send(200, result, "text/plain")
        if u.path == "/action" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            apply_action(db.connect(config.DB_PATH), int(f.get("id", 0) or 0), f.get("action", ""), f.get("email", ""))
            return self._send(303, "", headers=[("Location", "/")])
        if u.path == "/import" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            rows = importer.parse_csv(f.get("csv", "")) if f.get("mode") == "csv" else [f]
            results, truncated = importer.add_rows(con, rows)
            if any(st == "added" for _, st, _ in results):
                dedupe.run(con, log=lambda *_: None)
                # check/score/draft hit the network, so run them off the request thread
                threading.Thread(target=lambda: pipeline.process_new(db.connect(config.DB_PATH), log=print),
                                 daemon=True).start()
            return self._send(200, render_import_page(results, truncated))
        if u.path not in ("/action", "/import"):
            self._send(404, "Not found", "text/plain")


def tick(log=print, stop=None):
    """One scheduler pass: run the daily pipeline once per PH day, then send within the window."""
    con = db.connect(config.DB_PATH)
    now = datetime.now(timezone.utc)
    pht = now.astimezone(emailing.PHT)
    today = pht.date().isoformat()
    last = con.execute("SELECT value FROM meta WHERE key='last_pipeline'").fetchone()
    if pht.hour >= config.env_int("PIPELINE_HOUR_PHT", 7) and (not last or last[0] != today):
        con.execute("INSERT OR REPLACE INTO meta VALUES ('last_pipeline', ?)", (today,))
        con.commit()
        pipeline.run_daily(con, log=log, today=pht.date())
    if config.env("EMAIL_SENDING_ENABLED").lower() == "true":
        emailing.run_sender(con, log=log, sleep=stop.wait if stop else time.sleep)


def scheduler(stop, log=print):
    while not stop.is_set():
        try:
            tick(log, stop)
        except Exception as e:  # never let one bad run kill the loop
            log(f"scheduler error: {e!r}")
        stop.wait(60)


def serve():
    if len(config.env("ADMIN_PASSWORD")) < 12:
        raise SystemExit("Set ADMIN_PASSWORD (12+ characters) before starting the dashboard.")
    if config.env("EMAIL_SENDING_ENABLED").lower() == "true":
        missing = [k for k in ("RESEND_API_KEY", "SENDER_FROM_EMAIL", "UNSUB_SECRET", "RESEND_WEBHOOK_SECRET")
                   if not config.env(k)]
        if missing or not config.base_url():
            raise SystemExit(f"EMAIL_SENDING_ENABLED=true needs: {', '.join(missing + ([] if config.base_url() else ['BASE_URL']))}")
    db.connect(config.DB_PATH)  # create/upgrade the schema before serving
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(stop, lambda m: print(m, flush=True)), daemon=True).start()
    port = config.env_int("PORT", 8080)
    print(f"serving on :{port}", flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    finally:
        stop.set()
