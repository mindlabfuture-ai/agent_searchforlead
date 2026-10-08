import unittest

from leadagent import db, osm, pipeline


def el(name, i=1, **tags):
    return {"type": "node", "id": i, "tags": dict({"name": name, "shop": "clothes"}, **tags)}


class ToLeadTests(unittest.TestCase):
    def test_instagram_website_becomes_an_instagram_lead(self):
        self.assertEqual(osm.to_lead(el("Urban", website="https://instagram.com/urbanstore"))["url"], "https://instagram.com/urbanstore")

    def test_facebook_handle_tag_is_expanded(self):
        self.assertEqual(osm.to_lead(el("Glow", **{"contact:facebook": "glowph"}))["url"], "https://facebook.com/glowph")

    def test_own_domain_with_email_keeps_the_public_email(self):
        l = osm.to_lead(el("Memories", website="https://www.memories.ph/x", email="Hello@Memories.ph"))
        self.assertEqual((l["platform"], l["url"], l["email"]), ("web", "https://memories.ph", "hello@memories.ph"))
        self.assertIn("OpenStreetMap", l["email_source"])

    def test_phone_only_is_a_maps_lead_keyed_by_the_osm_object(self):
        l = osm.to_lead(el("Ming", 77, phone="+63 32 520 5852"))
        self.assertEqual(l["platform"], "maps"); self.assertEqual(l["url"], "https://www.openstreetmap.org/node/77")
        self.assertIn("+63 32 520 5852", l["snippet"])

    def test_junk_email_is_ignored_and_unnamed_or_disused_skipped(self):
        self.assertNotIn("email", osm.to_lead(el("A", website="https://a.ph", email="noreply@a.ph")))
        self.assertIsNone(osm.to_lead({"type": "node", "id": 1, "tags": {"shop": "clothes"}}))
        self.assertIsNone(osm.to_lead(el("Old", **{"disused:shop": "clothes"})))


class QueryTests(unittest.TestCase):
    def test_query_has_area_shop_types_and_contact_filter(self):
        q = osm.build_query("fashion", "Cebu City")
        self.assertIn('area["name"="Cebu City"]', q); self.assertIn("clothes", q); self.assertIn('["contact:facebook"~"."]', q)
        self.assertIn('ISO3166-1', osm.build_query("food", "Philippines"))

    def test_quotes_in_a_place_name_cannot_break_the_query(self):
        self.assertNotIn('""', osm.build_query("food", 'Ce"bu'))

    def test_daily_queries_rotate(self):
        from datetime import date
        self.assertNotEqual(osm.daily_queries(date(2026, 10, 8), 3), osm.daily_queries(date(2026, 10, 9), 3))


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(":memory:")

    def test_adds_leads_stores_email_and_dedupes_repeats(self):
        els = [el("A", 1, website="https://a.ph", email="hi@a.ph"), el("B", 2), el("A", 1, website="https://a.ph"), el("C", 3, **{"contact:facebook": "cc"})]
        n = osm.discover(self.con, queries=[("fashion", "Cebu")], fetch=lambda q: els, pause=0, log=lambda *_: None)
        self.assertEqual(n, 3)
        r = self.con.execute("SELECT email, source FROM leads WHERE url='https://a.ph'").fetchone()
        self.assertEqual((r["email"], r["source"]), ("hi@a.ph", "osm:fashion/Cebu"))

    def test_a_failing_query_is_logged_and_skipped(self):
        logs = []
        def fetch(q):
            if "beauty" in q or "cosmetics" in q: raise RuntimeError("busy")
            return [el("A", 1, website="https://a.ph")]
        n = osm.discover(self.con, queries=[("beauty", "Cebu"), ("fashion", "Cebu")], fetch=fetch, pause=0, log=logs.append)
        self.assertEqual(n, 1); self.assertTrue(any("busy" in m for m in logs))

    def test_osm_lead_without_a_store_qualifies(self):
        osm.discover(self.con, queries=[("fashion", "Cebu")], fetch=lambda q: [el("Glow", 5, **{"contact:facebook": "glow", "addr:city": "Cebu City Philippines"})], pause=0, log=lambda *_: None)
        self.con.execute("UPDATE leads SET shopify_status='no_store'"); self.con.commit()
        self.assertEqual(pipeline.score_all(self.con), 1)


if __name__ == "__main__":
    unittest.main()


class OverpassFallbackTests(unittest.TestCase):
    def test_falls_through_busy_and_broken_servers(self):
        seen = []
        def post(url, body):
            seen.append(url)
            if url == "a": raise RuntimeError("HTTP 500")
            if url == "b": return {"elements": [], "remark": "runtime error: open64"}
            return {"elements": [{"id": 1}]}
        self.assertEqual(osm.overpass("q", ["a", "b", "c"], post), [{"id": 1}])
        self.assertEqual(seen, ["a", "b", "c"])

    def test_raises_the_last_error_when_every_server_fails(self):
        def post(url, body): raise RuntimeError("busy " + url)
        with self.assertRaises(RuntimeError) as cm: osm.overpass("q", ["a", "b"], post)
        self.assertIn("busy b", str(cm.exception))

    def test_an_empty_answer_without_a_remark_is_a_real_empty_result(self):
        self.assertEqual(osm.overpass("q", ["a"], lambda u, b: {"elements": []}), [])
