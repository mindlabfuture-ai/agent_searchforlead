"""One small web service for Railway: approval dashboard, unsubscribe page, Resend webhook,
health check, and a background scheduler (daily pipeline + business-hours email sender)."""
import base64
import hashlib
import hmac
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import assistant, assistantchat, assistantui, config, inbox, inboxui, db, dedupe, demosite, emailing, followups, importer, pipeline, popload, poploadui, previews, previewui, search, showcase, themezip

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
    n_crit = assistantui.critical_count(con)
    crit = f" <b style='color:#a00'>({n_crit} critical)</b>" if n_crit else ""
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
        cards.append(f"""<section><h3>{E(r['name'] or r['url'])} <small>#{r['id']} &middot; {E(r['platform'])} &middot; score {r['score']} &middot; {E(r['status'])} &middot; <a href='/previews/{r['id']}'>Preview</a></small></h3>
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
            f"<h1>Lead queue</h1><p><a href='/import'>+ Import leads</a> &middot; <a href='/previews'>Previews</a> &middot; <a href='/prospects'>POPLoad prospects</a> &middot; <a href='/inbox'>Inbox</a> &middot; <a href='/assistant'>Assistant{crit}</a> &middot; <a href='/clients'>Clients</a></p><p class=b>{E(banner)}</p><p>{stats}</p>{''.join(cards) or '<p>No leads waiting.</p>'}")


def render_clients_page(con, message=""):
    """Clients we built a store for: add one at handover, watch the follow-up schedule, pause or skip."""
    sending = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    banner = ("LIVE: due follow-ups are sent Mon-Fri 9-17 PHT" if sending
              else "DRY RUN: follow-ups are not sent until EMAIL_SENDING_ENABLED=true")
    tok = f"<input type=hidden name=csrf value={csrf_token()}>"
    msg = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    today = followups.today_pht().isoformat()
    cards = []
    for c in con.execute("SELECT * FROM clients ORDER BY (status='active') DESC, id DESC LIMIT 200").fetchall():
        fu = con.execute("SELECT * FROM followups WHERE client_id=? ORDER BY due_at, id", (c["id"],)).fetchall()
        marks = {"sent": "sent", "skipped": "skipped", "failed": "FAILED", "pending": "due"}
        line = " &middot; ".join(f"{E(f['step'])}: {marks.get(f['status'], E(f['status']))} {E((f['sent_at'] or f['due_at'])[:10])}"
                                 f"{' (' + E(f['note']) + ')' if f['note'] and f['status'] != 'pending' else ''}" for f in fu)
        link = (f"<a href=\"{E(c['store_url'])}\" rel=\"noopener noreferrer\" target=_blank>{E(c['store_url'])}</a>"
                if c["store_url"] else "no store link")
        def btn(action, label):
            return (f"<form method=post action=/clients style=display:inline>{tok}<input type=hidden name=mode value=action>"
                    f"<input type=hidden name=client_id value={c['id']}><button name=action value={action}>{label}</button></form>")
        buttons = (btn("pause", "Pause") if c["status"] == "active" else btn("resume", "Resume") if c["status"] == "paused" else "")
        buttons += btn("skip_next", "Skip next") + btn("popload_installed", "POPLoad installed") + btn("popload_active", "POPLoad in use") \
            + (btn("done", "Mark done") if c["status"] != "done" else "")
        cards.append(f"<section><h3>{E(c['name'])} <small>#{c['id']} &middot; {E(c['status'])} &middot; "
                     f"POPLoad {E(c['popload_status'])}</small></h3><p>{link}<br>{E(c['email'])} &middot; handed over {E(c['handed_over_at'])}</p>"
                     f"<p><small>{line}</small></p>{buttons}</section>")
    stats = f"Follow-ups sent today: {followups.sent_today(con)}/{config.env_int('FOLLOWUP_DAILY_CAP', 20)}"
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Clients</title>{PAGE_STYLE}<p><a href='/'>&larr; Lead queue</a></p><h1>Clients</h1>"
            f"<p class=b style='background:{'#fde' if sending else '#ffd'}'>{E(banner)}</p><p>{stats}</p>{msg}"
            f"<section><h3>Add a client (after you hand over their store)</h3><form method=post action=/clients>{tok}<input type=hidden name=mode value=add>"
            f"<label>Business name</label><input type=text name=name required>"
            f"<label>Their business email (the one they asked you to use)</label><input type=text name=email required>"
            f"<label>Store link (https://...)</label><input type=text name=store_url>"
            f"<label>Handed over on (YYYY-MM-DD, blank = today)</label><input type=text name=handed_over value='{today}'>"
            f"<label>POPLoad</label><select name=popload_status><option value=not_installed>Not installed yet</option>"
            f"<option value=installed>Installed</option><option value=active>In use</option></select>"
            f"<label>Lead # (optional, from the queue; marks that lead as won)</label><input type=text name=lead_id>"
            f"<p><button>Add client and schedule follow-ups</button></p></form>"
            f"<p><small>Schedule: welcome on handover day, POPLoad check on day 7, growth tips on day 30, next-step offer on day 60. "
            f"Each is skipped if it is more than a week late, and no two go out within 3 days of each other.</small></p></section>"
            f"{''.join(cards) or '<p>No clients yet.</p>'}")


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
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' https: data:; frame-src 'self'; form-action 'self'")
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

    def _preview_page(self, con, lead_id, message=""):
        page = previewui.render_editor(con, lead_id, csrf_token(), message)
        return self._send(200, page) if page else self._send(404, "Unknown lead", "text/plain")

    def _post_preview_save(self, lead_id):
        """Multipart upload. Login is checked before the body is read, and the size is capped."""
        if not self._authed():
            return
        ctype, n = self.headers.get("Content-Type", ""), int(self.headers.get("Content-Length") or 0)
        if not ctype.startswith("multipart/form-data"):
            return self._send(400, "expected a form upload", "text/plain")
        if n > previewui.MAX_FORM_BYTES:
            return self._send(413, "That upload is too large (20 MB in total).", "text/plain")
        fields, files = previewui.parse_multipart(ctype, self.rfile.read(n))
        if not hmac.compare_digest(fields.get("csrf", ""), csrf_token()):
            return self._send(403, "bad csrf token", "text/plain")
        con = db.connect(config.DB_PATH)
        self._preview_page(con, lead_id, previewui.save_from_form(con, lead_id, fields, files))

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
        if u.path == "/inbox" and self._authed():
            return self._send(200, inboxui.render(db.connect(config.DB_PATH), csrf_token()))
        if u.path == "/assistant" and self._authed():
            return self._send(200, assistantui.render(db.connect(config.DB_PATH), csrf_token()))
        if u.path == "/prospects" and self._authed():
            show = parse_qs(u.query).get("show", ["verified"])[0]
            return self._send(200, poploadui.render_page(db.connect(config.DB_PATH), csrf_token(), show=show))
        if u.path == "/clients" and self._authed():
            return self._send(200, render_clients_page(db.connect(config.DB_PATH)))
        m = re.fullmatch(r"/i/([\w.]+)", u.path)
        if m:  # signed, unlisted image for the showcase email; never logged
            img = previews.image_from_token(db.connect(config.DB_PATH), m.group(1)) if len(config.env("UNSUB_SECRET")) >= 16 else None
            if not img:
                return self._send(404, "Not found", "text/plain")
            return self._send(200, img[1], img[0], headers=[("Cache-Control", "public, max-age=3600")])
        if u.path == "/previews" and self._authed():
            return self._send(200, previewui.render_list(db.connect(config.DB_PATH), csrf_token()))
        if u.path == "/previews/open" and self._authed():
            n = parse_qs(u.query).get("id", [""])[0].strip().lstrip("#")
            return self._send(303, "", headers=[("Location", f"/previews/{n}" if n.isdigit() else "/previews")])
        m = re.fullmatch(r"/previews/(\d+)", u.path)
        if m and self._authed():
            return self._preview_page(db.connect(config.DB_PATH), int(m.group(1)))
        m = re.fullmatch(r"/previews/(\d+)/(theme\.json|logo\.svg)", u.path)
        if m and self._authed():
            con = db.connect(config.DB_PATH)
            pv = previews.load(con, int(m.group(1)))
            if not pv:
                return self._send(404, "No preview yet", "text/plain")
            if m.group(2) == "theme.json":
                return self._send(200, json.dumps(previews.theme_settings(pv), indent=2), "application/json")
            return self._send(200, previews.generated_logo_svg(pv["name"], pv["brand"]), "image/svg+xml",
                              headers=[("Content-Disposition", "attachment; filename=logo.svg")])
        m = re.fullmatch(r"/previews/(\d+)/demo\.zip", u.path)
        if m and self._authed():
            data = demosite.site_zip(db.connect(config.DB_PATH), int(m.group(1)))
            if not data:
                return self._send(404, "No preview yet", "text/plain")
            return self._send(200, data, "application/zip", headers=[("Content-Disposition", "attachment; filename=demo-site.zip")])
        m = re.fullmatch(r"/previews/(\d+)/(theme\.zip|products\.csv|setup\.md)", u.path)
        if m and self._authed():  # the Shopify theme for this lead, its product import file and the setup guide
            if not config.base_url():
                return self._send(400, "BASE_URL is not set (the product import links to your hosted images)", "text/plain")
            zbytes, csv_text, guide, err = themezip.bundle(db.connect(config.DB_PATH), int(m.group(1)), config.base_url())
            if err:
                return self._send(404, err, "text/plain")
            kind = m.group(2)
            body, ctype = {"theme.zip": (zbytes, "application/zip"), "products.csv": (csv_text, "text/csv; charset=utf-8"),
                           "setup.md": (guide, "text/markdown; charset=utf-8")}[kind]
            return self._send(200, body, ctype, headers=[("Content-Disposition", f"attachment; filename={kind}")])
        m = re.fullmatch(r"/pimg/(\d+)/(\w+)", u.path)
        if m and self._authed():  # your uploaded screenshots and photos, behind the login only
            up = previews.get_uploads(db.connect(config.DB_PATH), int(m.group(1))).get(m.group(2))
            return self._send(200, up[1], up[0]) if up else self._send(404, "Not found", "text/plain")
        if u.path != "/":
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        m = re.fullmatch(r"/previews/(\d+)/save", u.path)
        if m:
            return self._post_preview_save(int(m.group(1)))
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
        if u.path == "/webhook/form":  # Netlify's outgoing webhook for the contact form; the token is the only credential
            tok = parse_qs(u.query).get("token", [""])[0]
            if not config.env("WEBHOOK_TOKEN") or not hmac.compare_digest(tok, config.env("WEBHOOK_TOKEN")):
                return self._send(401, "bad token", "text/plain")
            if inbox.enabled():
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    return self._send(400, "bad json", "text/plain")
                threading.Thread(target=inbox_form_job, args=(data,), daemon=True).start()
            return self._send(200, '{"ok": true}', "application/json")
        if u.path == "/inbox" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            pid = int(f["id"]) if (f.get("id") or "").isdigit() else 0
            message = ""
            if f.get("mode") == "send":
                message = inbox.send_pending(con, pid, inbox.Mailbox(), f.get("body")) if inbox.enabled() else "The inbox agent is off, so nothing can be sent."
            elif f.get("mode") == "skip":
                message = inbox.skip_pending(con, pid)
            return self._send(200, inboxui.render(con, csrf_token(), message))
        if u.path == "/assistant" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            message = answer = question = ""
            if f.get("mode") == "decide" and (f.get("id") or "").isdigit():
                message = assistant.decide(con, int(f["id"]), f.get("choice") == "confirm")
            elif f.get("mode") == "ask":
                question = f.get("question", "")
                answer, message = assistantchat.ask(con, question)
            return self._send(200, assistantui.render(con, csrf_token(), message, answer, question))
        if u.path == "/prospects" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            message, results = "", None
            if f.get("mode") == "import":
                results, _ = popload.add_rows(con, popload.parse_csv(f.get("csv", "")))
                if any(st == "added" for _, st, _ in results):  # checking sites hits the network: off the request thread
                    threading.Thread(target=lambda: popload.verify_all(db.connect(config.DB_PATH), log=print), daemon=True).start()
            elif f.get("mode") == "action" and (f.get("id") or "").isdigit():
                message = popload.apply_action(con, int(f["id"]), f.get("action", ""), f.get("value", ""))
            return self._send(200, poploadui.render_page(con, csrf_token(), message, results=results, show="all" if results else "verified"))
        if u.path == "/clients" and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            message = ""
            if f.get("mode") == "add":
                lead = f.get("lead_id", "").strip()
                cid, err = followups.add_client(
                    con, f.get("name"), f.get("email"), f.get("store_url"),
                    f.get("handed_over", "").strip() or None, int(lead) if lead.isdigit() else None,
                    f.get("popload_status", "not_installed"))
                message = f"Could not add: {err}" if err else "Client added. Follow-ups are scheduled."
            elif f.get("mode") == "action" and (f.get("client_id") or "").isdigit():
                followups.apply_action(con, int(f["client_id"]), f.get("action", ""))
            return self._send(200, render_clients_page(con, message))
        m = re.fullmatch(r"/previews/(\d+)/action", u.path)
        if m and self._authed():
            f = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            if not hmac.compare_digest(f.get("csrf", ""), csrf_token()):
                return self._send(403, "bad csrf token", "text/plain")
            con = db.connect(config.DB_PATH)
            return self._preview_page(con, int(m.group(1)), previewui.apply_action(con, int(m.group(1)), f.get("action", "")))
        if u.path not in ("/action", "/import", "/clients", "/prospects", "/assistant", "/inbox", "/webhook/form"):
            self._send(404, "Not found", "text/plain")


def inbox_form_job(data):
    """A website-form enquiry, handled off the request thread."""
    import anthropic
    try:
        inbox.form_enquiry(db.connect(config.DB_PATH), data, anthropic.Anthropic(), inbox.Mailbox(), inbox.Telegram())
    except Exception as e:
        print(f"form enquiry failed: {e!r}", flush=True)


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
    if pht.hour >= config.env_int("BRIEF_HOUR_PHT", 8):
        brief_day = con.execute("SELECT 1 FROM assistant_briefings WHERE day=?", (today,)).fetchone()
        if not brief_day:
            assistant.store_and_send(con, now, log=log)  # saves today's brief; emails it to OWNER_EMAIL if that is set
    assistant.alert_critical(con, now, log=log)          # a new critical finding is emailed once a day at most
    popload.adopt_shopify_leads(con, log=log)  # Shopify stores found by the store-build search become POPLoad prospects
    if (config.env("SERPER_API_KEY") or config.env("BRAVE_API_KEY")) and pht.hour >= config.env_int("PIPELINE_HOUR_PHT", 7):
        seen = con.execute("SELECT value FROM meta WHERE key='last_discovery'").fetchone()
        if not seen or seen[0] != today:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('last_discovery', ?)", (today,))
            con.commit()
            popload.discover(con, search.provider(), popload.discovery_queries(pht.date(), config.env_int("PROSPECT_SEARCH_QUERIES", 4)), log=log)
    popload.recheck_if_stale(con)
    popload.verify_all(con, log=lambda *_: None)  # checks any newly imported prospects (at most 60 per pass)
    if config.env("NETLIFY_AUTH_TOKEN") and con.execute("SELECT 1 FROM demo_sites WHERE status='live' LIMIT 1").fetchone():
        demosite.cleanup(con, log=log)  # expired demos, and demos of leads who opted out, come down
    showcase.prepare(con, log=log)  # builds previews a couple of days early; never approves or sends anything
    if config.env("EMAIL_SENDING_ENABLED").lower() == "true":
        wait = stop.wait if stop else time.sleep
        emailing.run_sender(con, log=log, sleep=wait)
        showcase.run(con, log=log, sleep=wait)
        followups.run(con, log=log, sleep=wait)
        popload.run(con, log=log, sleep=wait)


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
    inbox.start(stop, lambda m: print(m, flush=True))  # the inbox agent: off unless INBOX_ENABLED=true
    port = config.env_int("PORT", 8080)
    print(f"serving on :{port}", flush=True)
    try:
        ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
    finally:
        stop.set()
