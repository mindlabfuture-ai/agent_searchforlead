import base64, os, tempfile, threading, unittest, urllib.error, urllib.request
from http.server import ThreadingHTTPServer
from io import BytesIO
from unittest import mock

from PIL import Image

from leadagent import config, db, previews, server

ENV = {"UNSUB_SECRET": "x" * 32, "ADMIN_PASSWORD": "correct horse battery", "BASE_URL": "http://127.0.0.1"}


def png(color):
    b = BytesIO(); Image.new("RGB", (60, 60), color).save(b, "PNG"); return b.getvalue()


def multipart(fields, files):
    b = "BOUNDARY"; out = b""
    for k, v in fields.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (fn, data) in files.items():
        out += f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


@mock.patch.dict(os.environ, ENV)
class PreviewServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(config, "DB_PATH", os.path.join(self.tmp.name, "t.db")); self.patch.start()
        con = db.connect(config.DB_PATH)
        db.upsert_lead(con, "https://facebook.com/glowph", "Glow PH", "skincare")
        con.execute("UPDATE leads SET status='contacted', email='o@glow.ph'"); con.commit(); con.close()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"
        self.auth = "Basic " + base64.b64encode(b"admin:correct horse battery").decode()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.patch.stop(); self.tmp.cleanup()

    def call(self, path, data=None, ctype=None, auth=True):
        req = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET")
        if auth:
            req.add_header("Authorization", self.auth)
        if ctype:
            req.add_header("Content-Type", ctype)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_login_required(self):
        for p in ("/previews", "/previews/1", "/pimg/1/shot1", "/previews/1/theme.json"):
            self.assertEqual(self.call(p, auth=False)[0], 401, p)

    def test_bad_csrf_rejected(self):
        body, ct = multipart({"csrf": "bad"}, {})
        self.assertEqual(self.call("/previews/1/save", body, ct)[0], 403)

    def test_upload_flow_and_public_token(self):
        self.assertEqual(self.call("/previews/open?id=1")[0], 200)
        csrf = server.csrf_token() if hasattr(server, "csrf_token") else None
        import hashlib, hmac
        csrf = hmac.new(config.env("ADMIN_PASSWORD").encode(), b"csrf", hashlib.sha256).hexdigest()
        body, ct = multipart({"csrf": csrf, "brand": "nothex"}, {"up_p1": ("a.png", png("#0F9D9A")), "shot1": ("evil.png", b"<html>")})
        code, page, _ = self.call("/previews/1/save", body, ct)
        self.assertEqual(code, 200)
        self.assertIn(b"not a PNG", page)
        self.assertEqual(self.call("/pimg/1/up_p1")[0], 200)
        self.assertEqual(self.call("/previews/1/action", f"csrf={csrf}&action=approve".encode(), "application/x-www-form-urlencoded")[0], 200)
        con = db.connect(config.DB_PATH)
        con.execute("INSERT OR REPLACE INTO preview_images (lead_id,slot,content_type,data,source_url) VALUES (1,'p1','image/png',?,'x')", (png("red"),))
        con.commit(); con.close()
        tok = previews.image_token(1, "p1", 1)
        self.assertEqual(self.call(f"/i/{tok}", auth=False)[0], 200)
        self.assertEqual(self.call(f"/i/{tok[:-1]}0", auth=False)[0], 404)
        self.assertEqual(self.call(f"/i/{previews.image_token(1, 'shot1', 1)}", auth=False)[0], 404)


if __name__ == "__main__":
    unittest.main()


@mock.patch.dict(os.environ, ENV)
class DemoZipRouteTests(PreviewServerTests):
    def test_demo_zip_needs_login_and_returns_a_zip(self):
        self.assertEqual(self.call("/previews/1/demo.zip", auth=False)[0], 401)
        self.assertEqual(self.call("/previews/1/demo.zip")[0], 404)  # no preview yet
        con = db.connect(config.DB_PATH)
        previews.generate(con, con.execute("SELECT * FROM leads").fetchone(), lambda u: None, lambda u: None)
        code, data, headers = self.call("/previews/1/demo.zip")
        self.assertEqual((code, data[:2]), (200, b"PK")); self.assertEqual(headers["Content-Type"], "application/zip")


@mock.patch.dict(os.environ, ENV)
class ThemeRouteTests(PreviewServerTests):
    def test_theme_downloads_need_login_and_work(self):
        for name in ("theme.zip", "products.csv", "setup.md"):
            self.assertEqual(self.call(f"/previews/1/{name}", auth=False)[0], 401, name)
            self.assertEqual(self.call(f"/previews/1/{name}")[0], 404, name)  # no preview yet
        con = db.connect(config.DB_PATH)
        previews.generate(con, con.execute("SELECT * FROM leads").fetchone(), lambda u: None, lambda u: None)
        code, data, headers = self.call("/previews/1/theme.zip")
        self.assertEqual((code, data[:2], headers["Content-Type"]), (200, b"PK", "application/zip"))
        self.assertIn(b"Handle,Title", self.call("/previews/1/products.csv")[1])
        self.assertIn(b"Setting up the", self.call("/previews/1/setup.md")[1])


@mock.patch.dict(os.environ, ENV)
class AssistantRouteTests(PreviewServerTests):
    def test_assistant_page_and_actions(self):
        self.assertEqual(self.call("/assistant", auth=False)[0], 401)
        code, page, _ = self.call("/assistant"); self.assertEqual(code, 200); self.assertIn(b"Assistant", page)
        import hashlib, hmac
        csrf = hmac.new(config.env("ADMIN_PASSWORD").encode(), b"csrf", hashlib.sha256).hexdigest()
        self.assertEqual(self.call("/assistant", b"mode=decide&id=1&choice=confirm&csrf=bad", "application/x-www-form-urlencoded")[0], 403)
        from leadagent import assistant, popload
        con = db.connect(config.DB_PATH)
        popload.add_rows(con, [{"name": "A", "website": "a.ph"}]); con.execute("UPDATE prospects SET status='verified', email='a@a.ph'"); con.commit()
        pid, _ = assistant.propose(con, "prospect_reject", {"id": 1}, "test")
        code, page, _ = self.call("/assistant", f"mode=decide&id={pid}&choice=confirm&csrf={csrf}".encode(), "application/x-www-form-urlencoded")
        self.assertEqual(code, 200); self.assertIn(b"Rejected", page)
        self.assertEqual(db.connect(config.DB_PATH).execute("SELECT status FROM prospects").fetchone()[0], "rejected")
        code, page, _ = self.call("/assistant", f"mode=ask&question=hi&csrf={csrf}".encode(), "application/x-www-form-urlencoded")
        self.assertEqual(code, 200)


@mock.patch.dict(os.environ, {**ENV, "WEBHOOK_TOKEN": "form-secret", "INBOX_ENABLED": ""})
class InboxRouteTests(PreviewServerTests):
    def test_form_webhook_and_inbox_page(self):
        self.assertEqual(self.call("/webhook/form", b"{}", "application/json", auth=False)[0], 401)
        self.assertEqual(self.call("/webhook/form?token=wrong", b"{}", "application/json", auth=False)[0], 401)
        self.assertEqual(self.call("/webhook/form?token=form-secret", b'{"data": {"email": "a@b.ph"}}', "application/json", auth=False)[0], 200)
        with mock.patch.dict(os.environ, {"WEBHOOK_TOKEN": ""}):
            self.assertEqual(self.call("/webhook/form?token=", b"{}", "application/json", auth=False)[0], 401)  # no token configured: closed
        self.assertEqual(self.call("/inbox", auth=False)[0], 401)
        code, page, _ = self.call("/inbox"); self.assertEqual(code, 200); self.assertIn(b"Inbox", page)
        import hashlib, hmac
        csrf = hmac.new(config.env("ADMIN_PASSWORD").encode(), b"csrf", hashlib.sha256).hexdigest()
        self.assertEqual(self.call("/inbox", b"mode=skip&id=1&csrf=bad", "application/x-www-form-urlencoded")[0], 403)
        con = db.connect(config.DB_PATH)
        con.execute("INSERT INTO inbox_pending (email,subject,body,kind,ts) VALUES ('a@b.ph','s','b','reply','2026-10-14T00:00:00+00:00')"); con.commit()
        code, page, _ = self.call("/inbox", f"mode=skip&id=1&csrf={csrf}".encode(), "application/x-www-form-urlencoded")
        self.assertEqual((code, b"Skipped." in page), (200, True))
        con.execute("INSERT INTO inbox_pending (email,subject,body,kind,ts) VALUES ('a@b.ph','s','b','reply','2026-10-14T00:00:00+00:00')"); con.commit()
        code, page, _ = self.call("/inbox", f"mode=send&id=2&body=hi&csrf={csrf}".encode(), "application/x-www-form-urlencoded")
        self.assertIn(b"agent is off", page)                                                     # nothing is sent while the agent is off
