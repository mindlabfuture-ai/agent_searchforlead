import base64, hashlib, hmac, json, os, sqlite3, time, unittest
from datetime import datetime, timezone
from unittest import mock

from leadagent import config, db, emailing, pipeline, server, shopify_check

ENV = {"UNSUB_SECRET": "x" * 32, "ADMIN_PASSWORD": "correct horse battery", "BASE_URL": "https://leads.example.app",
       "SENDER_FROM_EMAIL": "hello@mail.example.ph", "EMAIL_DAILY_CAP": "2", "RESEND_API_KEY": "re_test"}
WED_10AM_PHT = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)   # Wed 10:00 PHT
SAT_10AM_PHT = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)


def plain(html):
    """Visible text of an HTML email: tags dropped, entities decoded (the bold lead-ins split phrases)."""
    import html as H, re
    return H.unescape(re.sub(r"<[^>]+>", "", html))


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

    def test_html_is_branded_safe_and_matches_text(self):
        con = mem(); l = lead(con, "glowph", name='Glow <img src=x onerror=alert(1)>\n\nPH & "Co"')
        con.execute("UPDATE leads SET url='https://facebook.com/glowph'"); l = con.execute("SELECT * FROM leads").fetchone()
        m = emailing.build_email(l, config.base_url()); h = m["html"]
        for brand in ("#060A12", "#0E1727", "#E3B965", "Space Grotesk", "https://mindlabfuture-ai.com/img/logo-ml.png", 'alt="MindLab Future AI"'):
            self.assertIn(brand, h)
        self.assertNotIn("<img src=x", h); self.assertNotIn("onerror=alert(1)>", h)
        self.assertEqual(h.count("<img"), 1)                              # only the logo: no tracking pixels
        self.assertNotIn("<script", h)
        self.assertIn("/unsubscribe?t=", h); self.assertIn("mailto:support@mindlabfuture-ai.com?subject=Libreng%20store%20preview%20para%20sa", h)
        self.assertEqual(m["subject"].count("\n"), 0); self.assertLessEqual(len(m["subject"]), 100)
        self.assertTrue(m["subject"].startswith("Simpleng online store para sa "))
        self.assertIn("Unsubscribe", h); self.assertIn("Taguig", h); self.assertIn('<html lang="tl">', h)
        self.assertGreater(len(m["text"]), 200)                           # plain-text part is still there

    def test_one_taglish_version_for_every_lead(self):
        """English-looking and Taglish-looking leads get the same email: no separate English version."""
        con = mem(); a = lead(con, "glowph", name="Glow PH")
        con.execute("UPDATE leads SET snippet='Skincare Manila. Order now, GCash, COD'")
        a = con.execute("SELECT * FROM leads").fetchone()
        con2 = mem(); lead(con2, "glowph", name="Glow PH"); con2.execute("UPDATE leads SET snippet='available po, mga kape po'")
        b = con2.execute("SELECT * FROM leads").fetchone()
        ma, mb = emailing.build_email(a, config.base_url()), emailing.build_email(b, config.base_url())
        strip = lambda m: m["text"].split("--\n")[0]
        self.assertEqual(strip(ma), strip(mb)); self.assertEqual(ma["subject"], mb["subject"])
        for part in (ma["text"], plain(ma["html"])):
            for english in ("I design and set up your store", "Would you like", "Get my free store preview", "You're getting this",
                            "A simple online store for", "Just reply to this email", "Unsubscribe or just reply"):
                self.assertNotIn(english, part)
        self.assertIn("Hi po Glow PH team,", ma["text"]); self.assertIn("Natanggap ninyo ang one-time na mensaheng ito", ma["text"])
        self.assertIn("(o mag-reply lang ng STOP)", ma["text"]); self.assertIn("o mag-reply lang ng STOP", ma["html"])

    def test_missing_name_falls_back_cleanly(self):
        con = mem(); lead(con, "noname", name=""); con.execute("UPDATE leads SET name=''")
        m = emailing.build_email(con.execute("SELECT * FROM leads").fetchone(), config.base_url())
        self.assertIn("Hi po Shop Owner team,", m["text"]); self.assertEqual(m["subject"], "Simpleng online store para sa Shop Owner")

    def test_marketplace_seller_gets_the_callout(self):
        con = mem(); l = lead(con, "kapeng", name="Kapeng Bukid")
        con.execute("UPDATE leads SET platform='shopee', url='https://shopee.ph/kapeng'")
        m = emailing.build_email(con.execute("SELECT * FROM leads").fetchone(), config.base_url())
        self.assertIn("Gusto ko ng libreng preview", m["html"]); self.assertIn("marketplace fees", m["html"])
        self.assertIn("Nakita ko po ang Shopee shop ninyo (https://shopee.ph/kapeng)", m["text"])
        self.assertIn("border-left:3px solid", m["html"])                  # shown as a callout, not buried in the pitch
        self.assertEqual(m["html"].count("marketplace fees"), 1)

    def test_offer_is_in_text_and_html(self):
        con = mem(); m = emailing.build_email(lead(con, "glowph"), config.base_url())
        for needle in ("Libreng store build: ako po ang magdidisenyo", "Maagang access sa POPLoad", "nasa review pa ng Shopify",
                       "hanggang 50 uploads", "kayo po ang pipili at magbabayad ng plan"):
            self.assertIn(needle, m["text"]); self.assertIn(needle, plain(m["html"]))
        self.assertIn("border:1px solid #E3B965", m["html"])               # offer card
        self.assertEqual(m["html"].count("&#10003;"), 3)
        self.assertIn("Libreng Shopify store build", m["html"])             # preheader

    def test_no_guarantee_or_refund_is_offered(self):
        con = mem(); m = emailing.build_email(lead(con, "glowph"), config.base_url())
        for part in (m["text"], plain(m["html"]), m["html"]):
            for banned in ("guarantee", "refund", "money-back", "₱1,000", "\u20b11,000", "ire-refund"):
                self.assertNotIn(banned, part.lower())

    def test_no_promo_claim_because_transferred_stores_are_not_eligible(self):
        con = mem(); l = lead(con, "glowph"); m = emailing.build_email(l, config.base_url())
        for part in (m["text"], plain(m["html"])):
            for banned in ("$1/month", "$1/buwan", "new-store deal", "3 days free", "free trial"):
                self.assertNotIn(banned, part)

    def test_partner_referral_is_disclosed(self):
        con = mem(); m = emailing.build_email(lead(con, "glowph"), config.base_url())
        self.assertIn("bilang Shopify Partner, maaari po akong makatanggap ng referral fee mula sa Shopify kapag nag-subscribe kayo", m["text"])
        self.assertEqual(plain(m["html"]).count("referral fee"), 1)         # shown once, not duplicated

    def test_domain_note_is_shown_in_both_parts(self):
        con = mem(); m = emailing.build_email(lead(con, "glowph"), config.base_url())
        for needle in ("hindi po kasama ang sariling domain name", "subdomain tulad ng yourshop.mindlabfuture-ai.com",
                       "lumipat sa sariling domain anumang oras"):
            self.assertIn(needle, m["text"]); self.assertIn(needle, plain(m["html"]))
        self.assertEqual(m["html"].count("&#10003;"), 3)                  # the note is not a fourth checked perk
        self.assertLess(m["text"].index("Shopify plan ninyo"), m["text"].index("Note: hindi po kasama"))
        self.assertLess(m["text"].index("Note: hindi po kasama"), m["text"].index("Gusto po ba ninyong makita"))

    def test_offer_survives_marketplace_variant(self):
        con = mem(); l = lead(con, "k"); con.execute("UPDATE leads SET platform='shopee', url='https://shopee.ph/k'")
        m = emailing.build_email(con.execute("SELECT * FROM leads").fetchone(), config.base_url())
        self.assertIn("Maagang access sa POPLoad", m["html"]); self.assertEqual(m["html"].count("&#10003;"), 3)
        self.assertIn("marketplace fees", m["html"])

    def test_logo_url_can_be_overridden(self):
        con = mem(); l = lead(con, "glowph")
        with mock.patch.dict(os.environ, {"LOGO_URL": "https://cdn.example.ph/logo.png"}):
            self.assertIn("https://cdn.example.ph/logo.png", emailing.build_email(l, config.base_url())["html"])

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
