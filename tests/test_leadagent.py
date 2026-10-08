import sqlite3
import unittest

from leadagent import db, dedupe, outreach, scoring, search, shopify_check


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


class T(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(db.normalize_fb_url("https://www.facebook.com/nellestore/posts/123"), "https://facebook.com/nellestore")
        self.assertEqual(db.normalize_fb_url("https://m.facebook.com/profile.php?id=99&x=1"), "https://facebook.com/profile.php?id=99")
        self.assertIsNone(db.normalize_fb_url("https://facebook.com/groups/abc"))
        self.assertIsNone(db.normalize_fb_url("https://example.com/x"))

    def test_normalize_platforms(self):
        n = db.normalize
        self.assertEqual(n("https://www.instagram.com/Glow.PH/?hl=en"), ("instagram", "https://instagram.com/glow.ph"))
        self.assertEqual(n("https://www.instagram.com/p/ABC/"), (None, None))
        self.assertEqual(n("https://www.tiktok.com/@shop.ph/video/123"), ("tiktok", "https://tiktok.com/@shop.ph"))
        self.assertEqual(n("https://shopee.ph/nelle_store?page=1"), ("shopee", "https://shopee.ph/nelle_store"))
        self.assertEqual(n("https://shopee.ph/Cool-Shirt-i.123.456"), (None, None))
        self.assertEqual(n("https://lazada.com.ph/shop/glow-ph/"), ("lazada", "https://lazada.com.ph/shop/glow-ph"))
        self.assertEqual(n("https://www.carousell.ph/u/jen/"), ("carousell", "https://carousell.ph/u/jen"))

    def test_marketplace_only_and_draft(self):
        self.assertEqual(shopify_check.check_website("", None, platform="shopee"), "marketplace_only")
        lead = dict(platform="shopee", name="Glow PH", snippet="skincare Manila GCash COD", shopify_status="marketplace_only")
        sc, notes = scoring.score_lead(lead)
        self.assertGreaterEqual(sc, scoring.QUALIFY_AT); self.assertIn("shopee only", notes)
        d = outreach.draft(lead)
        self.assertIn("Shopee shop", d); self.assertIn("marketplace fees", d); self.assertTrue(d.rstrip().endswith("again."))

    def test_auto_fallback(self):
        con = mem(); calls = []
        def fn(q):
            calls.append(q)
            if "facebook.com" in q: raise OSError("blocked")
            if "instagram.com" in q: return [{"url": "https://instagram.com/a_shop", "title": "A (@a_shop) • Instagram", "snippet": "x"}]
            return []
        added = search.run_auto(con, fn, niche="skincare", location="Cebu", max_queries=1, min_new=1, log=lambda *_: None)
        self.assertEqual(added, 1)
        self.assertFalse(any("tiktok" in q for q in calls))  # stopped once threshold met
        self.assertEqual(con.execute("SELECT name FROM leads").fetchone()[0], "A")

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

    def _add(self, con, url, name="", snippet="", website="", status=None, shopify=None):
        db.upsert_lead(con, url, name, snippet, website)
        lid = con.execute("SELECT id FROM leads WHERE url=?", (db.normalize(url)[1],)).fetchone()[0]
        if status: con.execute("UPDATE leads SET status=? WHERE id=?", (status, lid))
        if shopify: con.execute("UPDATE leads SET shopify_status=? WHERE id=?", (shopify, lid))
        return lid

    def test_merge_same_handle_across_platforms(self):
        con = mem()
        fb = self._add(con, "https://facebook.com/glowph", "Glow PH", "skincare Manila COD", status="drafted")
        ig = self._add(con, "https://instagram.com/glow.ph", "Glow PH", "skincare DM to order GCash")
        sp = self._add(con, "https://shopee.ph/glow_ph", "Glow", "shop", shopify="marketplace_only")
        other = self._add(con, "https://facebook.com/someoneelse", "Someone Else")
        groups, _ = dedupe.run(con, log=lambda *_: None)
        self.assertEqual(len(groups), 1)
        p = con.execute("SELECT * FROM leads WHERE id=?", (fb,)).fetchone()  # most advanced status is kept
        self.assertEqual(p["status"], "drafted")
        self.assertIn("instagram:https://instagram.com/glow.ph", p["also_on"])
        self.assertIn("shopee:https://shopee.ph/glow_ph", p["also_on"])
        self.assertIn("DM to order", p["snippet"]); self.assertEqual(p["shopify_status"], "marketplace_only")
        for i in (ig, sp):
            r = con.execute("SELECT * FROM leads WHERE id=?", (i,)).fetchone()
            self.assertEqual((r["status"], r["merged_into"]), ("merged", fb))
        self.assertEqual(con.execute("SELECT status FROM leads WHERE id=?", (other,)).fetchone()[0], "new")
        self.assertFalse(db.upsert_lead(con, "https://instagram.com/glow.ph"))  # still deduped on re-discovery

    def test_merge_same_website_and_shopify_wins(self):
        con = mem()
        a = self._add(con, "https://facebook.com/abc", "A", website="https://www.brand.ph/shop", shopify="no_store")
        b = self._add(con, "https://instagram.com/xyz", "B", website="https://brand.ph", shopify="has_shopify")
        self._add(con, "https://facebook.com/other1", "C", website="https://bit.ly/x")
        self._add(con, "https://instagram.com/other2", "D", website="https://bit.ly/y")
        groups, _ = dedupe.run(con, log=lambda *_: None)
        self.assertEqual(len(groups), 1)
        p = con.execute("SELECT * FROM leads WHERE status!='merged' AND id IN (?,?)", (a, b)).fetchone()
        self.assertEqual(p["shopify_status"], "has_shopify")
        self.assertEqual(scoring.score_lead(p)[0], 0)  # any Shopify profile disqualifies the business

    def test_weak_name_match_is_review_only(self):
        con = mem()
        self._add(con, "https://facebook.com/page1", "Kapeng Bukid")
        self._add(con, "https://instagram.com/kb_davao", "Kapeng Bukid")
        groups, possibles = dedupe.run(con, log=lambda *_: None)
        self.assertEqual((len(groups), len(possibles)), (0, 1))
        a, b, _ = possibles[0]
        dedupe.merge_rows(con, [a, b])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM leads WHERE status='merged'").fetchone()[0], 1)

    def test_do_not_contact_covers_merged_profiles(self):
        con = mem()
        self._add(con, "https://facebook.com/glowph", "Glow", status="do_not_contact")
        self._add(con, "https://instagram.com/glow.ph", "Glow")
        dedupe.run(con, log=lambda *_: None)
        self.assertFalse(db.upsert_lead(con, "https://instagram.com/glow.ph"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM do_not_contact").fetchone()[0], 2)

    def test_multi_platform_bonus(self):
        base = dict(platform="instagram", name="Glow PH", snippet="skincare Manila GCash COD", shopify_status="no_store")
        self.assertGreater(scoring.score_lead({**base, "also_on": "shopee:https://shopee.ph/x"})[0],
                           scoring.score_lead(base)[0])


if __name__ == "__main__":
    unittest.main()
