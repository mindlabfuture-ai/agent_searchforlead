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


def prospect(con, status="verified", email="hello@glowph.com"):
    popload.add_rows(con, [{"name": "Glow PH", "website": "www.glowph.com", "niche": "skincare", "claim": "x"}])
    con.execute("UPDATE prospects SET status=?, email=?, pay_methods='GCash, bank transfer', platform='has_shopify', pay_level='proof'", (status, email))
    con.commit()
    return con.execute("SELECT * FROM prospects").fetchone()


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

    def test_three_steps_on_schedule_once_each(self):
        con = mem(); p = prospect(con)
        self.assertIn("Approved", popload.apply_action(con, p["id"], "approve"))
        n, calls = self.sent(con); self.assertEqual(n, 1)
        self.assertEqual(calls[0][1], f"prospect-{p['id']}-intro")
        self.assertIn("List-Unsubscribe", calls[0][0]["headers"])
        self.assertEqual(self.sent(con)[0], 0)                                  # same day: nothing more
        self.assertEqual(self.sent(con, NOW + timedelta(days=2))[0], 0)
        n, calls = self.sent(con, NOW + timedelta(days=5)); self.assertEqual((n, calls[0][1]), (1, f"prospect-{p['id']}-how_it_works"))
        n, calls = self.sent(con, NOW + timedelta(days=12)); self.assertEqual((n, calls[0][1]), (1, f"prospect-{p['id']}-last_note"))
        self.assertEqual(self.sent(con, NOW + timedelta(days=20))[0], 0)

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
