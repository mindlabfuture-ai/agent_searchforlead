import base64, hashlib, hmac, json, os, sqlite3, time, unittest
from datetime import datetime, timezone
from unittest import mock

from leadagent import config, db, emailing, pipeline, server, shopify_check

ENV = {"UNSUB_SECRET": "x" * 32, "ADMIN_PASSWORD": "correct horse battery", "BASE_URL": "https://leads.example.app",
       "SENDER_FROM_EMAIL": "hello@mail.example.ph", "EMAIL_DAILY_CAP": "2", "RESEND_API_KEY": "re_test"}
WED_10AM_PHT = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)   # Wed 10:00 PHT
SAT_10AM_PHT = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def lead(con, slug, email="owner@shop.ph", status="approved", name="Glow PH", score=80):
    db.upsert_lead(con, f"https://facebook.com/{slug}", name, "skincare Manila")
    con.execute("UPDATE leads SET status=?, email=?, email_source='https://shop.ph', score=? WHERE url LIKE ?",
                (status, email, score, f"%/{slug}"))
    return con.execute("SELECT * FROM leads WHERE url LIKE ?", (f"%/{slug}",)).fetchone()


@mock.patch.dict(os.environ, ENV)
class EmailTests(unittest.TestCase):
    def test_extract_emails_filters_junk(self):
        txt = 'Contact <a href="mailto:Hello@GlowPH.com">us</a> logo@2x.png noreply@brand.com a@sentry.io hello@glowph.com'
        self.assertEqual(emailing.extract_emails(txt), ["hello@glowph.com"])

    def test_unsub_token_roundtrip_and_tamper(self):
        t = emailing.unsub_token("A@b.ph")
        self.assertEqual(emailing.parse_unsub_token(t), "a@b.ph")
        self.assertIsNone(emailing.parse_unsub_token(t[:-1] + ("0" if t[-1] != "0" else "1")))
        self.assertIsNone(emailing.parse_unsub_token("garbage"))

    def test_message_has_compliance_elements(self):
        con = mem(); m = emailing.build_email(lead(con, "glowph"), config.base_url())
        self.assertIn("/unsubscribe?t=", m["text"]); self.assertIn("Taguig", m["text"])
        self.assertEqual(m["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertIn("List-Unsubscribe", m["headers"])

    def test_dry_run_sends_nothing_and_keeps_status(self):
        con = mem(); l = lead(con, "glowph"); calls = []
        r = emailing.send_one(con, l, post=lambda p, k: calls.append(p), enabled=False, log=lambda *_: None)
        self.assertEqual((r, calls), ("dry_run", []))
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "approved")

    def test_live_send_logs_marks_contacted_and_never_repeats(self):
        con = mem(); l = lead(con, "glowph"); sent = []
        post = lambda p, k: sent.append((p, k)) or {"id": "re_1"}
        self.assertEqual(emailing.send_one(con, l, post=post, enabled=True), "sent")
        self.assertEqual(sent[0][1], f"lead-{l['id']}-initial"); self.assertEqual(sent[0][0]["to"], ["owner@shop.ph"])
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "contacted")
        con.execute("UPDATE leads SET status='approved'")  # even if re-approved, same lead/address is blocked
        again = con.execute("SELECT * FROM leads").fetchone()
        self.assertEqual(emailing.send_one(con, again, post=post, enabled=True), "skipped: already emailed")
        self.assertEqual(len(sent), 1)

    def test_blocks_unapproved_suppressed_and_other_leads_with_same_address(self):
        con = mem()
        self.assertEqual(emailing.can_send(con, lead(con, "a", status="drafted"))[1], "not approved")
        emailing.suppress(con, "x@y.ph", "unsubscribed")
        # suppression is by address, so it also covers leads discovered later with the same email
        self.assertEqual(emailing.can_send(con, lead(con, "b", email="X@y.ph"))[1], "suppressed")
        con2 = mem(); c = lead(con2, "c", email="c@y.ph")
        db.add_do_not_contact(con2, c["url"], "asked to stop")
        stale = {**dict(c), "status": "approved"}  # e.g. a copy loaded before the opt-out landed
        self.assertEqual(emailing.can_send(con2, stale)[1], "do not contact")

    def test_failed_send_is_retryable_but_limited(self):
        con = mem(); l = lead(con, "glowph")
        def boom(p, k): raise OSError("net down")
        for _ in range(emailing.MAX_FAILURES):
            self.assertEqual(emailing.send_one(con, l, post=boom, enabled=True), "failed")
        self.assertEqual(emailing.send_one(con, l, post=boom, enabled=True), "skipped: too many failures")

    def test_daily_cap_and_window(self):
        con = mem(); [lead(con, f"s{i}", email=f"o{i}@shop.ph", score=90 - i) for i in range(4)]
        post = lambda p, k: {"id": k}
        n = emailing.run_sender(con, post=post, now=WED_10AM_PHT, enabled=True, log=lambda *_: None, sleep=lambda s: None)
        self.assertEqual(n, 2)  # EMAIL_DAILY_CAP=2
        self.assertEqual(emailing.run_sender(con, post=post, now=WED_10AM_PHT, enabled=True, log=lambda *_: None, sleep=lambda s: None), 0)
        self.assertEqual(emailing.run_sender(mem(), now=SAT_10AM_PHT), 0)
        self.assertFalse(emailing.in_send_window(SAT_10AM_PHT)); self.assertTrue(emailing.in_send_window(WED_10AM_PHT))

    def test_missing_base_url_refuses_to_send(self):
        con = mem(); l = lead(con, "glowph")
        with mock.patch.dict(os.environ, {"BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertTrue(emailing.send_one(con, l, enabled=True).startswith("skipped: BASE_URL"))


class WebhookTests(unittest.TestCase):
    def sign(self, secret, body, ts=None, msg_id="msg_1"):
        ts = str(ts or int(time.time()))
        key = base64.b64decode(secret.split("_", 1)[1])
        sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
        return {"svix-id": msg_id, "svix-timestamp": ts, "svix-signature": f"v1,{sig}"}

    def test_svix_verify(self):
        secret = "whsec_" + base64.b64encode(b"k" * 24).decode(); body = b'{"type":"email.bounced"}'
        self.assertTrue(emailing.verify_svix(secret, self.sign(secret, body), body))
        self.assertFalse(emailing.verify_svix(secret, self.sign(secret, body), body + b" "))
        self.assertFalse(emailing.verify_svix(secret, self.sign(secret, body, ts=int(time.time()) - 3600), body))
        self.assertFalse(emailing.verify_svix("", self.sign(secret, body), body))

    @mock.patch.dict(os.environ, ENV)
    def test_bounce_and_complaint_suppress_and_opt_out(self):
        con = mem(); l = lead(con, "glowph", status="contacted")
        con.execute("INSERT INTO emails (lead_id,to_email,resend_id,status) VALUES (?,?,?,?)", (l["id"], "owner@shop.ph", "re_9", "sent"))
        ev = {"type": "email.complained", "data": {"email_id": "re_9", "to": ["Owner@shop.ph"]}}
        self.assertEqual(emailing.handle_resend_event(con, ev), "complained")
        self.assertTrue(emailing.is_suppressed(con, "owner@shop.ph"))
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "do_not_contact")
        self.assertEqual(con.execute("SELECT status FROM emails").fetchone()[0], "complained")
        self.assertEqual(emailing.handle_resend_event(con, {"type": "email.opened", "data": {}}), "ignored")


@mock.patch.dict(os.environ, ENV)
class ServerTests(unittest.TestCase):
    def test_dashboard_escapes_untrusted_lead_data(self):
        con = mem(); l = lead(con, "glowph", status="drafted", name="<script>alert(1)</script>")
        con.execute("UPDATE leads SET snippet='<img src=x onerror=alert(1)>', score_notes='<b>x</b>', draft='<script>d</script>'")
        out = server.render_dashboard(con)
        for raw in ("<script>alert(1)</script>", "<img src=x", "<script>d</script>", "<b>x</b>"):
            self.assertNotIn(raw, out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", out); self.assertIn("DRY RUN", out)

    def test_actions(self):
        con = mem(); l = lead(con, "glowph", status="drafted")
        server.apply_action(con, l["id"], "approve")
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "approved")
        server.apply_action(con, l["id"], "dnc")
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "do_not_contact")
        self.assertTrue(emailing.is_suppressed(con, "owner@shop.ph"))
        server.apply_action(con, l["id"], "approve")  # cannot resurrect an opted-out lead
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "do_not_contact")

    def test_cannot_approve_without_email_or_set_junk_email(self):
        con = mem(); l = lead(con, "glowph", email=None, status="drafted")
        server.apply_action(con, l["id"], "approve")
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "drafted")
        server.apply_action(con, l["id"], "set_email", "noreply@brand.com")
        self.assertIsNone(con.execute("SELECT email FROM leads").fetchone()[0])
        server.apply_action(con, l["id"], "set_email", "Owner@Shop.ph")
        self.assertEqual(con.execute("SELECT email FROM leads").fetchone()[0], "owner@shop.ph")


class PipelineTests(unittest.TestCase):
    def test_check_all_picks_up_public_email_and_skips_shopify_sites(self):
        con = mem()
        db.upsert_lead(con, "https://facebook.com/glowph", "Glow PH", "skincare Manila", "https://glow.example.ph")
        db.upsert_lead(con, "https://facebook.com/shopifyco", "Shop Co", "x", "https://shopco.example.ph")
        pages = {"https://glow.example.ph": ("<p>Email us: hello@glow.example.ph</p>", {}),
                 "https://shopco.example.ph": ('<script src="https://cdn.shopify.com/a.js"></script> hi@shopco.ph', {})}
        real = shopify_check.inspect_website
        with mock.patch.object(shopify_check, "inspect_website",
                               lambda w, fetch_fn=None, platform="facebook": real(w, pages.get, platform)):
            pipeline.check_all(con, log=lambda *_: None)
        rows = {r["name"]: r for r in con.execute("SELECT * FROM leads")}
        self.assertEqual(rows["Glow PH"]["email"], "hello@glow.example.ph")
        self.assertEqual(rows["Glow PH"]["email_source"], "https://glow.example.ph")
        self.assertIsNone(rows["Shop Co"]["email"])  # already on Shopify: not a lead, collect nothing

    def test_rotation_is_deterministic(self):
        from datetime import date
        self.assertEqual(pipeline.todays_focus(date(2026, 10, 8)), pipeline.todays_focus(date(2026, 10, 8)))
        self.assertNotEqual(pipeline.todays_focus(date(2026, 10, 8)), pipeline.todays_focus(date(2026, 10, 9)))


if __name__ == "__main__":
    unittest.main()
