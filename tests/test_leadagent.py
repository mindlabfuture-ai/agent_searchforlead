import sqlite3
import unittest

from leadagent import db, outreach, scoring, search, shopify_check


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


class T(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(db.normalize_fb_url("https://www.facebook.com/nellestore/posts/123"), "https://facebook.com/nellestore")
        self.assertEqual(db.normalize_fb_url("https://m.facebook.com/profile.php?id=99&x=1"), "https://facebook.com/profile.php?id=99")
        self.assertIsNone(db.normalize_fb_url("https://facebook.com/groups/abc"))
        self.assertIsNone(db.normalize_fb_url("https://example.com/x"))

    def test_dedupe_and_dnc(self):
        con = mem()
        self.assertTrue(db.upsert_lead(con, "https://facebook.com/a"))
        self.assertFalse(db.upsert_lead(con, "https://www.facebook.com/a/photos"))
        db.add_do_not_contact(con, "https://facebook.com/b")
        self.assertFalse(db.upsert_lead(con, "https://facebook.com/b"))

    def test_shopify_detect(self):
        self.assertEqual(shopify_check.classify_html('<script src="https://cdn.shopify.com/x.js">'), "has_shopify")
        self.assertEqual(shopify_check.classify_html("<html>wix site</html>"), "no_store")
        self.assertEqual(shopify_check.check_website("", None), "no_store")
        self.assertEqual(shopify_check.check_website("https://shopee.ph/x", None), "marketplace_only")
        def boom(u): raise OSError()
        self.assertEqual(shopify_check.check_website("shop.com", boom), "unknown")

    def test_extract_website(self):
        self.assertEqual(search.extract_website("Visit https://facebook.com/x or https://mybrand.ph/shop."), "https://mybrand.ph/shop")

    def test_scoring(self):
        lead = dict(name="Nelle Sportswear", snippet="Activewear Manila. DM to order, GCash, COD, available po", shopify_status="no_store")
        sc, _ = scoring.score_lead(lead)
        self.assertGreaterEqual(sc, scoring.QUALIFY_AT)
        lead["shopify_status"] = "has_shopify"
        self.assertEqual(scoring.score_lead(lead)[0], 0)

    def test_draft_language(self):
        tl = dict(name="Ate Jen", snippet="available po, mga items lang po")
        self.assertIn("po", outreach.draft(tl))
        self.assertIn("STOP", outreach.draft(dict(name="X", snippet="")))


if __name__ == "__main__":
    unittest.main()
