import os, sqlite3, unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from leadagent import db, emailing, popload, poploadui

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app", "SENDER_FROM_EMAIL": "hello@mail.example.ph",
       "RESEND_API_KEY": "re_test", "PROSPECT_DAILY_CAP": "2"}
NOW = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)  # Wed 10:00 PHT
SHOP = '<html><script src="https://cdn.shopify.com/x.js"></script>Pay by GCash or bank transfer. Please send proof of payment to <a href="mailto:Hello@GlowPH.com">us</a></html>'


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def fetcher(pages):
    def fetch(url):
        path = url[len("https://glowph.com"):] or "/"
        if url.startswith("https://glowph.com") and path in pages:
            return pages[path]
        raise ValueError("blocked: host does not resolve")
    return fetch


def prospect(con, status="verified", email="hello@glowph.com", name="Glow PH", website="www.glowph.com"):
    popload.add_rows(con, [{"name": name, "website": website, "niche": "skincare", "claim": "x"}])
    con.execute("UPDATE prospects SET status=?, email=?, pay_methods='GCash, bank transfer', platform='has_shopify', pay_level='proof' WHERE name=?", (status, email, name))
    con.commit()
    return con.execute("SELECT * FROM prospects WHERE name=?", (name,)).fetchone()


@mock.patch.dict(os.environ, ENV)
class ImportVerifyTests(unittest.TestCase):
    def test_parse_gemini_header_and_dedupe(self):
        con = mem()
        rows = popload.parse_csv('Merchant Brand,Niche / Products,Platform / Domain,Manual Payment Instructions\n"Glow PH","skin","https://www.glowph.com","x"\n"Glow Again","skin","glowph.com","x"\n"","","bad",""\n')
        results, _ = popload.add_rows(con, rows)
        self.assertEqual([s for _, s, _ in results], ["added", "skipped", "skipped"])
        self.assertEqual(con.execute("SELECT domain FROM prospects").fetchone()[0], "glowph.com")

    def test_skips_store_build_leads(self):
        con = mem(); db.upsert_lead(con, "https://facebook.com/glow", "Glow", "", "https://glowph.com")
        results, _ = popload.add_rows(con, [{"name": "Glow", "website": "glowph.com"}])
        self.assertIn("lead", results[0][2])

    def test_verified_when_shopify_payments_and_email(self):
        con = mem(); popload.add_rows(con, [{"name": "Glow PH", "website": "glowph.com"}])
        popload.verify_all(con, fetch=fetcher({"/": (SHOP, {})}), log=lambda *_: None)
        p = con.execute("SELECT * FROM prospects").fetchone()
        self.assertEqual((p["status"], p["email"], p["pay_level"]), ("verified", "hello@glowph.com", "proof"))

    def test_rejections(self):
        for html, why in (('<html>plain site gcash</html>', "not a Shopify"), (SHOP.replace("GCash", "x").replace("bank transfer", "y"), "no sign")):
            con = mem(); popload.add_rows(con, [{"name": "G", "website": "glowph.com"}])
            popload.verify_all(con, fetch=fetcher({"/": (html, {})}), log=lambda *_: None)
            self.assertIn(why, con.execute("SELECT reject_reason FROM prospects").fetchone()[0])
        con = mem(); popload.add_rows(con, [{"name": "G", "website": "glowph.com"}])
        popload.verify_all(con, fetch=fetcher({}), log=lambda *_: None)
        self.assertIn("did not load", con.execute("SELECT reject_reason FROM prospects").fetchone()[0])

    def test_needs_email_then_manual_email(self):
        con = mem(); popload.add_rows(con, [{"name": "G", "website": "glowph.com"}])
        popload.verify_all(con, fetch=fetcher({"/": ("cdn.shopify.com GCash", {})}), log=lambda *_: None)
        pid = con.execute("SELECT id, status FROM prospects").fetchone()
        self.assertEqual(pid["status"], "needs_email")
        popload.apply_action(con, pid["id"], "email", "owner@glowph.com")
        self.assertEqual(con.execute("SELECT status FROM prospects").fetchone()[0], "verified")

    def test_spreadsheet_tabs_and_semicolons_import_like_commas(self):
        for d in ("\t", ";", ","):
            text = d.join(["Merchant Brand", "Niche / Products", "Platform / Domain", "Manual Payment Instructions"]) + "\n" + d.join(["Glow PH", "skin", "glowph.com", "x"]) + "\n"
            con = mem(); results, _ = popload.add_rows(con, popload.parse_csv("\ufeff" + text))
            self.assertEqual([(n, s) for n, s, _ in results], [("Glow PH", "added")], repr(d))
        self.assertEqual(popload.parse_csv('a,b "x; y"\n1,2')[0].get("website"), None)  # a plain comma file is still comma-split

    def test_email_filters(self):
        self.assertFalse(popload.plausible_email("xxx@xxx.xxx"))
        self.assertFalse(popload.plausible_email("shop@mrsgarcias.com.phcontact"))
        self.assertTrue(popload.plausible_email("shop@mrsgarcias.com.ph"))
        self.assertEqual(popload.pick_emails(["a@gmail.com", "info@glowph.com"], "glowph.com")[0], "info@glowph.com")

    def test_payment_assessment_uses_word_boundaries(self):
        self.assertEqual(popload.assess_payments("<p>we love bpickles</p>")[0], "none")
        self.assertEqual(popload.assess_payments("GCash only")[0], "mention")


@mock.patch.dict(os.environ, ENV)
class SequenceTests(unittest.TestCase):
    def sent(self, con, now=NOW, **kw):
        calls = []
        n = popload.run(con, post=lambda p, k: calls.append((p, k)) or {"id": f"r{len(calls)}"}, now=now, enabled=True, log=lambda *_: None, **kw)
        return n, calls

    def test_nothing_without_approval(self):
        con = mem(); prospect(con)
        self.assertEqual(self.sent(con)[0], 0)

    @mock.patch.dict(os.environ, {"POPLOAD_DEMO_URL": "https://loom.example/abc"})
    def test_four_touches_on_schedule_once_each(self):
        con = mem(); p = prospect(con)
        self.assertIn("Approved", popload.apply_action(con, p["id"], "approve"))
        n, calls = self.sent(con); self.assertEqual(n, 1)
        self.assertEqual(calls[0][1], f"prospect-{p['id']}-intro")
        self.assertIn("List-Unsubscribe", calls[0][0]["headers"])
        self.assertEqual(self.sent(con)[0], 0)                                  # same day: nothing more
        self.assertEqual(self.sent(con, NOW + timedelta(days=2))[0], 0)
        n, calls = self.sent(con, NOW + timedelta(days=5)); self.assertEqual((n, calls[0][1]), (1, f"prospect-{p['id']}-reminder"))
        n, calls = self.sent(con, NOW + timedelta(days=9)); self.assertEqual((n, calls[0][1]), (1, f"prospect-{p['id']}-demo"))
        n, calls = self.sent(con, NOW + timedelta(days=15)); self.assertEqual((n, calls[0][1]), (1, f"prospect-{p['id']}-last_note"))
        self.assertEqual(self.sent(con, NOW + timedelta(days=30))[0], 0)

    def test_demo_step_is_skipped_without_a_video_and_links_it_with_one(self):
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve"); self.sent(con)
        self.sent(con, NOW + timedelta(days=5))
        with mock.patch.dict(os.environ, {"POPLOAD_DEMO_URL": ""}):
            n, calls = self.sent(con, NOW + timedelta(days=9))                  # demo skipped, nothing due
            self.assertEqual(n, 0)
        self.assertEqual(con.execute("SELECT status FROM prospect_steps WHERE step='demo'").fetchone()[0], "skipped")
        con = mem(); p = prospect(con)
        with mock.patch.dict(os.environ, {"POPLOAD_DEMO_URL": "https://loom.example/abc"}):
            m = popload.build_message(p, "demo", "https://leads.example.app")
        self.assertIn("https://loom.example/abc", m["html"]); self.assertIn("Watch it here: https://loom.example/abc", m["text"])

    def test_prospects_approved_before_a_step_existed_still_get_it(self):
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve")
        con.execute("DELETE FROM prospect_steps WHERE step IN ('reminder','demo')"); con.commit()
        self.sent(con); n, calls = self.sent(con, NOW + timedelta(days=5))
        self.assertEqual(calls[0][1], f"prospect-{p['id']}-reminder")

    def test_highest_fit_goes_first_when_the_cap_is_short(self):
        con = mem(); a = prospect(con, name="Low", website="low.ph", email="a@low.ph"); b = prospect(con, name="Hot", website="hot.ph", email="a@hot.ph")
        con.execute("UPDATE prospects SET pain_score=14 WHERE id=?", (b["id"],)); con.commit()
        for x in (a, b): popload.apply_action(con, x["id"], "approve")
        with mock.patch.dict(os.environ, {"PROSPECT_DAILY_CAP": "1"}):
            n, calls = self.sent(con)
        self.assertEqual(calls[0][1], f"prospect-{b['id']}-intro")

    def test_reply_unsubscribe_and_late_stop_the_sequence(self):
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve"); self.sent(con)
        popload.apply_action(con, p["id"], "replied")
        self.assertEqual(self.sent(con, NOW + timedelta(days=5))[0], 0)
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve"); self.sent(con)
        emailing.suppress(con, "hello@glowph.com", "unsubscribed")
        self.assertEqual(self.sent(con, NOW + timedelta(days=5))[0], 0)
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve"); self.sent(con)
        self.assertEqual(self.sent(con, NOW + timedelta(days=30))[0], 0)       # far too late: skipped, not sent late

    def test_dry_run_window_and_cap(self):
        con = mem(); p = prospect(con); popload.apply_action(con, p["id"], "approve")
        post = mock.Mock()
        self.assertEqual(popload.run(con, post=post, now=NOW, enabled=False, log=lambda *_: None), 0)
        self.assertEqual(popload.run(con, post=post, now=datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc), enabled=True, log=lambda *_: None), 0)
        post.assert_not_called()

    def test_approve_guards(self):
        con = mem(); p = prospect(con, status="rejected")
        self.assertIn("verified", popload.apply_action(con, p["id"], "approve"))
        con = mem(); p = prospect(con); emailing.suppress(con, "hello@glowph.com", "x")
        self.assertIn("opted out", popload.apply_action(con, p["id"], "approve"))
        con = mem(); p = prospect(con)
        con.execute("INSERT INTO emails (lead_id,to_email,subject,status,sent_at,kind) VALUES (1,'hello@glowph.com','s','sent','2026-10-01','initial')"); con.commit()
        self.assertIn("already emailed", popload.apply_action(con, p["id"], "approve"))

    def test_message_content_and_escaping(self):
        con = mem(); p = prospect(con)
        for step, _ in popload.STEPS:
            m = popload.build_message(p, step, "https://leads.example.app")
            self.assertIn("unsubscribe", m["html"].lower()); self.assertIn("Unsubscribe", m["text"])
            self.assertNotIn("{", m["subject"])
        con.execute("UPDATE prospects SET name='<script>x</script>'"); con.commit()
        m = popload.build_message(con.execute("SELECT * FROM prospects").fetchone(), "intro", "https://leads.example.app")
        self.assertNotIn("<script>x", m["html"])

    def test_page_renders_and_escapes(self):
        con = mem(); prospect(con)
        con.execute("UPDATE prospects SET name='<img src=x onerror=1>', claim='<b>'"); con.commit()
        page = poploadui.render_page(con, "tok")
        self.assertNotIn("<img src=x", page); self.assertIn("Approve sequence", page)


if __name__ == "__main__":
    unittest.main()


@mock.patch.dict(os.environ, ENV)
class DiscoveryTests(unittest.TestCase):
    def test_adopts_shopify_leads_once(self):
        con = mem()
        db.upsert_lead(con, "https://facebook.com/a", "Glow", "skin", "https://glowph.com")
        db.upsert_lead(con, "https://facebook.com/b", "Plain", "x", "https://plain.ph")
        db.upsert_lead(con, "https://facebook.com/c", "Opted", "x", "https://optedout.ph")
        con.execute("UPDATE leads SET shopify_status='has_shopify' WHERE url LIKE '%/a' OR url LIKE '%/c'")
        db.add_do_not_contact(con, "https://facebook.com/c", "asked")
        con.commit()
        self.assertEqual(popload.adopt_shopify_leads(con, log=lambda *_: None), 1)
        self.assertEqual(popload.adopt_shopify_leads(con, log=lambda *_: None), 0)
        self.assertEqual(con.execute("SELECT domain, status FROM prospects").fetchall()[0][:], ("glowph.com", "new"))

    def test_discover_filters_hosts_and_dupes(self):
        con = mem(); popload.add_rows(con, [{"name": "Old", "website": "old.ph"}])
        results = [{"url": "https://www.facebook.com/x", "title": "x", "snippet": ""},
                   {"url": "https://old.ph/pages/payment", "title": "Old", "snippet": ""},
                   {"url": "https://glowph.com/pages/payment?x=1", "title": "Glow PH - Payment | Glow", "snippet": "GCash"},
                   {"url": "https://glowph.com/other", "title": "Glow again", "snippet": ""},
                   {"url": "https://en.wikipedia.org/wiki/x", "title": "w", "snippet": ""}]
        n = popload.discover(con, lambda q: results, ["q"], log=lambda *_: None)
        self.assertEqual(n, 1)
        self.assertEqual(con.execute("SELECT name, domain FROM prospects WHERE domain='glowph.com'").fetchone()[:], ("Glow PH", "glowph.com"))

    def test_discover_survives_a_failing_query(self):
        con = mem()
        def boom(q): raise OSError("rate limited")
        self.assertEqual(popload.discover(con, boom, ["a", "b"], log=lambda *_: None), 0)

    def test_queries_rotate_daily(self):
        from datetime import date
        a, b = popload.discovery_queries(date(2026, 10, 8)), popload.discovery_queries(date(2026, 10, 9))
        self.assertEqual(len(a), 4); self.assertNotEqual(a, b); self.assertTrue(all("{" not in q for q in a))


@mock.patch.dict(os.environ, ENV)
class PainTests(unittest.TestCase):
    def test_detects_proof_habits_from_site_text(self):
        t = "<p>Paid by BDO or GCash? Email a photo of your deposit slip within 24 hours with your order number. We ship only after payment is verified.</p>"
        self.assertEqual(set(popload.detect_pain(t)), {"email", "order_no", "deadline", "before_dispatch"})
        self.assertIn("messenger", popload.detect_pain("Send your GCash screenshot to our Facebook page Messenger."))
        self.assertEqual(popload.detect_pain("<p>We sell candles. Contact us anytime.</p><script>email proof</script>"), [])

    def test_score_and_class_follow_the_strategy_weights(self):
        self.assertEqual(popload.pain_score(["email", "messenger", "order_no", "before_dispatch", "deadline"], "has_shopify", "proof"), 17)
        self.assertEqual(popload.pain_score([], "has_shopify", "mention"), 5)
        self.assertEqual([popload.pain_class(x) for x in (12, 8, 5, 4)], ["Hot", "Warm", "Potential", "Low"])

    def test_opening_line_states_only_what_was_seen(self):
        con = mem(); p = prospect(con)
        con.execute("UPDATE prospects SET pay_level='mention'"); con.commit(); p = con.execute("SELECT * FROM prospects").fetchone()
        self.assertIn("match them to orders by hand", popload.observation(p, "Glow"))   # nothing specific: general line
        con.execute("UPDATE prospects SET pain='email'"); con.commit(); p = con.execute("SELECT * FROM prospects").fetchone()
        self.assertIn("email their payment proof", popload.build_message(p, "intro", "https://x.app")["text"])
        con.execute("UPDATE prospects SET pain='messenger'"); con.commit(); p = con.execute("SELECT * FROM prospects").fetchone()
        m = popload.build_message(p, "intro", "https://x.app")["text"]
        self.assertIn("through Messenger", m); self.assertIn("not the best place to keep payment receipts", m)
        self.assertIn("through Messenger", popload.build_message(p, "reminder", "https://x.app")["text"])

    def test_verification_stores_pain_and_score(self):
        con = mem(); popload.add_rows(con, [{"name": "G", "website": "glowph.com"}])
        page = "cdn.shopify.com GCash. Email your deposit slip with your order number to hello@glowph.com within 24 hours."
        popload.verify_all(con, fetch=fetcher({"/": (page, {})}), log=lambda *_: None)
        r = con.execute("SELECT pain, pain_score, status FROM prospects").fetchone()
        self.assertIn("email", r["pain"]); self.assertGreaterEqual(r["pain_score"], 12); self.assertEqual(r["status"], "verified")


@mock.patch.dict(os.environ, ENV)
class CrawlTests(unittest.TestCase):
    def test_follows_the_shops_own_payment_links_only(self):
        home = ('<a href="/pages/how-we-accept-payment">p</a> <a href="https://www.glowph.com/pages/faq-shipping">f</a>'
                '<a href="https://evil.example/pages/payment">x</a> <a href="/products/payment-mug">no</a> <a href="/">home</a>')
        got = popload.linked_pages(home, "https://glowph.com")
        self.assertEqual(got, ["https://glowph.com/pages/how-we-accept-payment", "https://www.glowph.com/pages/faq-shipping"])

    def test_payment_wording_on_a_linked_page_is_found(self):
        con = mem(); popload.add_rows(con, [{"name": "G", "website": "glowph.com"}])
        pages = {"/": ('cdn.shopify.com <a href="/pages/how-we-accept-payment">x</a>', {}),
                 "/pages/how-we-accept-payment": ("GCash or BDO transfer: email the deposit slip to hello@glowph.com", {})}
        popload.verify_all(con, fetch=fetcher(pages), log=lambda *_: None)
        r = con.execute("SELECT status, pay_level, pain, email_source FROM prospects").fetchone()
        self.assertEqual((r["status"], r["pay_level"]), ("verified", "proof")); self.assertIn("email", r["pain"])
        self.assertTrue(r["email_source"].endswith("/pages/how-we-accept-payment"))

    def test_a_slow_home_page_gets_one_retry(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1: raise OSError("timeout")
            return ("cdn.shopify.com GCash hello@glowph.com", {})
        self.assertEqual(popload.verify_site({"website": "https://glowph.com", "domain": "glowph.com"}, fetch)["platform"], "has_shopify")
