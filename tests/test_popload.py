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
        self.assertIn("through social media messages", m); self.assertIn("not the best place to keep payment receipts", m)
        self.assertIn("through social media messages", popload.build_message(p, "reminder", "https://x.app")["text"])

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


@mock.patch.dict(os.environ, ENV)
class LinkListTests(unittest.TestCase):
    def test_plain_list_of_links_one_per_line(self):
        con = mem()
        text = "https://www.pink-manila.ph/products/x?utm=1\nglowph.com\n\nhttps://instagram.com/somestore\nhttps://l.facebook.com/x\n"
        results, _ = popload.add_rows(con, popload.parse_csv(text))
        self.assertEqual([(s) for _, s, _ in results], ["added", "added", "skipped", "skipped"])
        self.assertEqual([r["name"] for r in con.execute("SELECT name FROM prospects ORDER BY id")], ["Pink Manila", "Glowph"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospects WHERE name_auto=1").fetchone()[0], 2)
        self.assertIn("own website", results[2][2])

    def test_several_links_on_one_line_and_mixed_separators(self):
        for text in ("a.ph b.ph c.ph", "a.ph, b.ph, c.ph", "a.ph\tb.ph\tc.ph", "a.ph;b.ph;c.ph"):
            rows = popload.parse_csv(text)
            self.assertEqual(sorted(r["website"] for r in rows), ["a.ph", "b.ph", "c.ph"], repr(text))

    def test_headerless_rows_keep_name_niche_and_notes_wherever_the_link_is(self):
        self.assertEqual(popload.parse_csv("Glow PH,skin,glowph.com,takes GCash")[0],
                         {"name": "Glow PH", "niche": "skin", "claim": "takes GCash", "website": "glowph.com"})
        self.assertEqual(popload.parse_csv("Glow PH,glowph.com")[0], {"name": "Glow PH", "website": "glowph.com"})

    def test_verification_replaces_a_placeholder_name_with_the_shops_own(self):
        con = mem(); popload.add_rows(con, popload.parse_csv("glowph.com"))
        page = '<title>Glow PH – Skincare | Home</title><meta property="og:site_name" content="Glow Skin PH"> cdn.shopify.com GCash hello@glowph.com'
        popload.verify_all(con, fetch=fetcher({"/": (page, {})}), log=lambda *_: None)
        r = con.execute("SELECT name, name_auto FROM prospects").fetchone()
        self.assertEqual((r["name"], r["name_auto"]), ("Glow Skin PH", 0))

    def test_a_name_you_typed_is_never_replaced(self):
        con = mem(); popload.add_rows(con, [{"name": "My Pick", "website": "glowph.com"}])
        popload.verify_all(con, fetch=fetcher({"/": ("<title>Other Name</title> cdn.shopify.com GCash hello@glowph.com", {})}), log=lambda *_: None)
        self.assertEqual(con.execute("SELECT name FROM prospects").fetchone()[0], "My Pick")

    def test_site_name_cleanup(self):
        self.assertEqual(popload.site_name("<title>Pink Manila &amp; Co - Welcome</title>"), "Pink Manila & Co")
        self.assertEqual(popload.site_name("<title>Home</title>"), "")
        self.assertEqual(popload.site_name("<title>x</title>"), "")
        self.assertEqual(popload.site_name("no title here"), "")

    def test_placeholder_name_uses_the_brand_label_not_a_subdomain(self):
        self.assertEqual([popload.name_from_domain(d) for d in ("shop.brand-name.com.ph", "pinkmanila.ph", "a-b_c.com")], ["Brand Name", "Pinkmanila", "A B C"])


class ChatDetectionTests(unittest.TestCase):
    def test_social_links_in_a_menu_are_not_a_way_to_send_proof(self):
        menu = "Log in Sign up Facebook Instagram Home Payment Channels Have an unpaid order? Settle through GCash. Email proof of payment to hello@x.ph with your order number."
        self.assertNotIn("messenger", popload.detect_pain(menu)); self.assertIn("email", popload.detect_pain(menu))

    def test_real_chat_wording_is_still_found(self):
        for t in ("Please send a clear copy of the proof of payment via e-mail or Facebook for us to process your order.",
                  "Send your GCash screenshot to our Messenger.", "DM us your payment receipt with your order number.",
                  "Proof of payment may be sent through Viber."):
            self.assertIn("messenger", popload.detect_pain(t), t)


class ProofWordingTests(unittest.TestCase):
    def test_receipt_of_an_item_or_a_returns_email_is_not_payment_proof(self):
        for t in ("If an item is damaged on receipt of your order, email us within 14 days.",
                  "Contact us within 7 days of receipt at help@x.ph. Include your order number.",
                  "You will need the receipt or proof of purchase. To start a return, contact us at a@x.ph.",
                  "Save the payment instructions by taking a screenshot, or send them to your email.",
                  "We collect your name, billing address, payment confirmation, email address and phone number."):
            self.assertEqual(popload.detect_pain(t), [], t)

    def test_real_proof_wording_is_found(self):
        for t in ("Please email a screenshot of the transaction to a@x.ph.", "Send your deposit slip to a@x.ph",
                  "kindly email proof of payment to a@x.ph", "Include a GCash screenshot when you email us at a@x.ph"):
            self.assertIn("email", popload.detect_pain(t), t)


@mock.patch.dict(os.environ, ENV)
class RecheckTests(unittest.TestCase):
    def test_old_results_are_rechecked_once_and_your_decisions_stay(self):
        con = mem()
        for i, (st, why, src) in enumerate([("verified", None, "https://a.ph/p"), ("rejected", "no sign of GCash", None), ("rejected", "rejected by you", None),
                                            ("needs_email", None, None), ("verified", None, "added manually"), ("approved", None, "https://f.ph/p")]):
            con.execute("INSERT INTO prospects (name,website,domain,status,reject_reason,email_source) VALUES (?,?,?,?,?,?)",
                        (f"P{i}", f"https://p{i}.ph", f"p{i}.ph", st, why, src))
        con.commit()
        self.assertEqual(popload.recheck_if_stale(con), 3)
        got = {r["name"]: r["status"] for r in con.execute("SELECT name,status FROM prospects")}
        self.assertEqual(got, {"P0": "new", "P1": "new", "P2": "rejected", "P3": "new", "P4": "verified", "P5": "approved"})
        self.assertEqual(popload.recheck_if_stale(con), 0)                      # only once per detector version
        con.execute("UPDATE prospects SET status='verified' WHERE name='P0'"); con.commit()
        self.assertEqual(popload.recheck_if_stale(con), 0); self.assertEqual(con.execute("SELECT status FROM prospects WHERE name='P0'").fetchone()[0], "verified")


@mock.patch.dict(os.environ, ENV)
class HandlePersonallyTests(unittest.TestCase):
    def two(self, con):
        a = prospect(con, name="Alpha", website="alpha.ph", email="a@alpha.ph"); b = prospect(con, name="Beta", website="beta.ph", email="b@beta.ph")
        return a, b

    def test_a_card_can_be_taken_out_of_the_sequence_and_moved_back(self):
        con = mem(); a, b = self.two(con)
        popload.apply_action(con, a["id"], "approve")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospect_steps WHERE prospect_id=? AND status='pending'", (a["id"],)).fetchone()[0], 4)
        self.assertIn("Handled personally", popload.apply_action(con, a["id"], "manual"))
        self.assertEqual(con.execute("SELECT status FROM prospects WHERE id=?", (a["id"],)).fetchone()[0], "manual")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospect_steps WHERE prospect_id=? AND status='pending'", (a["id"],)).fetchone()[0], 0)
        calls = []
        n = popload.run(con, post=lambda p, k: calls.append(k) or {"id": "r"}, now=NOW, enabled=True, log=lambda *_: None)
        self.assertEqual((n, calls), (0, []))                                            # nothing is ever sent to them
        self.assertEqual(popload.apply_action(con, a["id"], "approve"), "Only verified prospects with an email can be approved.")
        popload.apply_action(con, a["id"], "reconsider")
        self.assertEqual(con.execute("SELECT status FROM prospects WHERE id=?", (a["id"],)).fetchone()[0], "verified")

    def test_hold_all_moves_the_waiting_ones_and_leaves_the_rest(self):
        con = mem(); a, b = self.two(con)
        prospect(con, name="Gamma", website="gamma.ph", email="g@gamma.ph", status="rejected")
        popload.apply_action(con, b["id"], "approve")
        self.assertEqual(popload.hold_ready(con), 1)
        got = {r["name"]: r["status"] for r in con.execute("SELECT name,status FROM prospects")}
        self.assertEqual(got, {"Alpha": "manual", "Beta": "approved", "Gamma": "rejected"})

    def test_a_recheck_does_not_touch_handled_prospects(self):
        con = mem(); a, b = self.two(con); popload.hold_ready(con)
        self.assertEqual(popload.recheck_if_stale(con), 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospects WHERE status='manual'").fetchone()[0], 2)


@mock.patch.dict(os.environ, ENV)
class ProspectExportTests(unittest.TestCase):
    def test_csv_has_the_useful_columns_and_defuses_formulas(self):
        from leadagent import poploadui
        con = mem(); p = prospect(con, name="=HYPERLINK(\"http://evil\")", website="alpha.ph", email="a@alpha.ph")
        con.execute("UPDATE prospects SET pain='email,order_no', pain_score=12, email_source='https://alpha.ph/pages/contact', claim='+cmd'"); con.commit()
        prospect(con, name="Other", website="beta.ph", email="b@beta.ph", status="rejected")
        import csv, io
        rows = list(csv.DictReader(io.StringIO(poploadui.export_csv(con, "verified"))))
        self.assertEqual(len(rows), 1); r = rows[0]
        self.assertTrue(r["Store"].startswith("'=")); self.assertTrue(r["Your list says"].startswith("'+"))
        self.assertEqual((r["Email"], r["Fit"], r["Fit score"], r["How they ask for proof of payment"]), ("a@alpha.ph", "Hot", "12", "by email, order number needed"))
        self.assertEqual(len(list(csv.DictReader(io.StringIO(poploadui.export_csv(con, "all"))))), 2)

    def test_page_offers_the_download_the_filter_and_the_bulk_button(self):
        from leadagent import poploadui
        con = mem(); prospect(con)
        page = poploadui.render_page(con, "tok", show="verified")
        for want in ("/prospects.csv?show=verified", "Handled personally", "Take all 1 waiting prospects out of the email sequence", "Handle personally"):
            self.assertIn(want, page)


@mock.patch.dict(os.environ, {**ENV, "POPLOAD_DEMO_URL": "https://loom.example/abc"})
class EmailPreviewTests(unittest.TestCase):
    def test_preview_shows_all_four_emails_and_cannot_unsubscribe_anyone(self):
        from leadagent import poploadui
        con = mem(); p = prospect(con)
        con.execute("UPDATE prospects SET pain='email'"); con.commit()
        page = poploadui.render_emails(con, p["id"], "https://leads.example.app")
        for want in ("Day 0: intro", "Day 3: reminder", "Day 7: demo", "Day 14: last note", "Chasing payment screenshots at Glow PH?", "https://loom.example/abc", "email their payment proof"):
            self.assertIn(want, page)
        self.assertNotIn("/unsubscribe?t=", page)                                         # no live opt-out link anywhere in the preview
        self.assertIn("<iframe sandbox", page)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospect_steps").fetchone()[0], 0)   # looking changes nothing

    def test_preview_works_without_an_email_and_for_unknown_ids(self):
        from leadagent import poploadui
        con = mem(); p = prospect(con, email=None)
        page = poploadui.render_emails(con, p["id"], "https://leads.example.app")
        self.assertIn("no email found yet", page); self.assertIsNone(poploadui.render_emails(con, 999, "https://x"))
        self.assertIn("Preview the emails", poploadui.render_page(con, "tok", show="verified"))


@mock.patch.dict(os.environ, ENV)
class LearnMoreLinkTests(unittest.TestCase):
    def test_the_popload_page_is_linked_in_every_email_but_the_last_note(self):
        con = mem(); p = prospect(con)
        for step, _ in popload.STEPS:
            m = popload.build_message(p, step, "https://leads.example.app")
            has = "More about POPLoad: https://mindlabfuture-ai.com/popload/" in m["text"] and 'href="https://mindlabfuture-ai.com/popload/"' in m["html"]
            self.assertEqual(has, step != "last_note", step)

    def test_the_address_can_be_changed_and_is_escaped(self):
        con = mem(); p = prospect(con)
        with mock.patch.dict(os.environ, {"POPLOAD_URL": 'https://x.example/pop?a=1&b="2"'}):
            m = popload.build_message(p, "intro", "https://leads.example.app")
        self.assertIn("&amp;b=&quot;2&quot;", m["html"]); self.assertNotIn('b="2"', m["html"])
