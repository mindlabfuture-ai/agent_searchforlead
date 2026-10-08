import unittest

from leadagent import db, pipeline, places, scoring


def place(name, site="", phone="", status="OPERATIONAL", pid="p1"):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": "Makati, Metro Manila, Philippines",
            "websiteUri": site, "nationalPhoneNumber": phone, "businessStatus": status,
            "primaryTypeDisplayName": {"text": "Candle store"}}


class ToLeadTests(unittest.TestCase):
    def test_facebook_site_becomes_a_facebook_lead(self):
        l = places.to_lead(place("Glow", "https://www.facebook.com/glowcandles.ph/?ref=x"))
        self.assertEqual(l["url"], "https://facebook.com/glowcandles.ph"); self.assertNotIn("platform", l)

    def test_own_domain_becomes_a_web_lead(self):
        l = places.to_lead(place("Glow", "https://www.glow.ph/shop?x=1"))
        self.assertEqual((l["platform"], l["url"], l["website"]), ("web", "https://glow.ph", "https://glow.ph"))

    def test_no_website_becomes_a_maps_lead_with_phone_in_snippet(self):
        l = places.to_lead(place("Glow", "", "0917 123 4567", pid="abc"))
        self.assertEqual(l["platform"], "maps"); self.assertIn("place_id:abc", l["url"]); self.assertIn("0917 123 4567", l["snippet"])

    def test_link_in_bio_site_is_not_treated_as_an_own_domain(self):
        self.assertEqual(places.to_lead(place("Glow", "https://linktr.ee/glow"))["platform"], "maps")

    def test_closed_or_nameless_places_are_skipped(self):
        self.assertIsNone(places.to_lead(place("Glow", status="CLOSED_PERMANENTLY")))
        self.assertIsNone(places.to_lead(place("")))


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")

    def test_adds_leads_follows_pages_and_dedupes(self):
        pages = {None: ([place("A", "https://a.ph", pid="a"), place("B", pid="b")], "t2"), "t2": ([place("A", "https://a.ph", pid="a"), place("C", "https://facebook.com/cc")], None)}
        n = places.discover(self.con, key="k", queries=["q"], search_fn=lambda q, k, t: pages[t], log=lambda *_: None)
        self.assertEqual(n, 3)
        self.assertEqual({r["platform"] for r in self.con.execute("SELECT platform FROM leads")}, {"web", "maps", "facebook"})

    def test_do_not_contact_is_respected(self):
        db.add_do_not_contact(self.con, "https://a.ph")
        n = places.discover(self.con, key="k", queries=["q"], search_fn=lambda q, k, t: ([place("A", "https://a.ph")], None), log=lambda *_: None)
        self.assertEqual(n, 0)

    def test_a_failing_query_is_logged_and_does_not_stop_the_rest(self):
        logs = []
        def fn(q, k, t):
            if q == "bad": raise RuntimeError("quota")
            return [place("A", "https://a.ph")], None
        self.assertEqual(places.discover(self.con, key="k", queries=["bad", "good"], search_fn=fn, log=logs.append), 1)
        self.assertTrue(any("quota" in m for m in logs))

    def test_page_limit(self):
        calls = []
        def fn(q, k, t): calls.append(t); return [], "next"
        places.discover(self.con, key="k", queries=["q"], max_pages=2, search_fn=fn, log=lambda *_: None)
        self.assertEqual(len(calls), 2)

    def test_daily_queries_rotate(self):
        from datetime import date
        a, b = places.daily_queries(date(2026, 10, 8), 3), places.daily_queries(date(2026, 10, 9), 3)
        self.assertEqual(len(set(a)), 3); self.assertNotEqual(a, b)


class ScoringTests(unittest.TestCase):
    def test_places_lead_without_a_store_qualifies(self):
        con = db.connect(":memory:")
        l = places.to_lead(place("Glow", "", pid="x")); l["source"] = "places:q"
        db.upsert_lead(con, **l)
        con.execute("UPDATE leads SET shopify_status='no_store'"); con.commit()
        self.assertEqual(pipeline.score_all(con), 1)

    def test_shopify_store_found_by_places_is_still_dropped(self):
        r = {"name": "Glow", "snippet": "Makati, Philippines", "also_on": "", "platform": "web", "shopify_status": "has_shopify", "source": "places:q"}
        self.assertEqual(scoring.score_lead(r)[0], 0)


if __name__ == "__main__":
    unittest.main()
