import os, sqlite3, unittest
from unittest import mock

from leadagent import db, emailing, importer, pipeline, scoring, server, shopify_check

ENV = {"UNSUB_SECRET": "x" * 32, "ADMIN_PASSWORD": "correct horse battery"}


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


@mock.patch.dict(os.environ, ENV)
class ImportTests(unittest.TestCase):
    def test_add_lead_ok_and_reasons(self):
        con = mem()
        self.assertEqual(importer.add_lead(con, "https://www.facebook.com/glowph/posts/1", "Glow PH", email="Owner@Glow.ph"), ("added", ""))
        row = con.execute("SELECT * FROM leads").fetchone()
        self.assertEqual((row["url"], row["email"], row["email_source"], row["source"]),
                         ("https://facebook.com/glowph", "owner@glow.ph", "added manually", "dashboard"))
        self.assertEqual(importer.add_lead(con, "https://facebook.com/glowph")[1], "already in the list")
        self.assertIn("not a supported", importer.add_lead(con, "https://example.com/x")[1])
        db.add_do_not_contact(con, "https://instagram.com/gone", "asked")
        self.assertEqual(importer.add_lead(con, "https://instagram.com/gone")[1], "opted out earlier")
        emailing.suppress(con, "bad@x.ph", "bounced")
        self.assertIn("opted out or bounced", importer.add_lead(con, "https://instagram.com/other", email="bad@x.ph")[1])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM leads WHERE url LIKE '%other'").fetchone()[0], 0)

    def test_junk_email_and_website_are_dropped_not_fatal(self):
        con = mem()
        st, note = importer.add_lead(con, "https://instagram.com/shop1", website="javascript:alert(1)", email="noreply@brand.com")
        row = con.execute("SELECT * FROM leads").fetchone()
        self.assertEqual((st, row["website"], row["email"]), ("added", "", None)); self.assertIn("email ignored", note)

    def test_parse_csv_with_and_without_header(self):
        self.assertEqual(importer.parse_csv("https://facebook.com/a,A,notes,,a@b.ph\nhttps://instagram.com/b,B")[1]["name"], "B")
        r = importer.parse_csv("fb_url,name,website\nhttps://facebook.com/a,A,a.ph\n")
        self.assertEqual((r[0]["url"], r[0]["website"]), ("https://facebook.com/a", "a.ph"))
        self.assertEqual(importer.parse_csv("\n \n"), [])

    def test_row_limit(self):
        con = mem(); rows = [{"url": f"https://instagram.com/shop{i:04d}"} for i in range(importer.MAX_ROWS + 5)]
        results, truncated = importer.add_rows(con, rows)
        self.assertEqual((len(results), truncated), (importer.MAX_ROWS, True))

    def test_handadded_lead_stays_in_queue_but_shopify_is_excluded(self):
        con = mem()
        importer.add_lead(con, "https://facebook.com/plainshop", "Plain Shop")            # weak signals, low score
        importer.add_lead(con, "https://facebook.com/shopifyshop", "Shopify Shop")
        con.execute("UPDATE leads SET shopify_status='has_shopify' WHERE url LIKE '%shopifyshop'")
        con.execute("UPDATE leads SET shopify_status='no_store' WHERE url LIKE '%plainshop'")
        pipeline.score_all(con)
        st = {r["name"]: r["status"] for r in con.execute("SELECT * FROM leads")}
        self.assertEqual(st, {"Plain Shop": "qualified", "Shopify Shop": "new"})
        con2 = mem(); db.upsert_lead(con2, "https://facebook.com/plainshop", "Plain Shop", source="search:x")
        con2.execute("UPDATE leads SET shopify_status='no_store'"); pipeline.score_all(con2)
        self.assertEqual(con2.execute("SELECT status FROM leads").fetchone()[0], "new")  # searched leads still need 50+

    def test_import_page_escapes_results_and_has_csrf(self):
        page = server.render_import_page([("<script>x</script>", "skipped", "<b>why</b>")], truncated=True)
        self.assertNotIn("<script>x</script>", page); self.assertNotIn("<b>why</b>", page)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", page); self.assertIn(server.csrf_token(), page)


class SsrfTests(unittest.TestCase):
    def test_blocks_internal_targets(self):
        for url in ("http://127.0.0.1/", "http://10.1.2.3/", "http://192.168.0.5/", "http://169.254.169.254/latest/meta-data/",
                    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "file:///etc/passwd", "ftp://8.8.8.8/",
                    "http://8.8.8.8:8080/", "https://user@127.0.0.1/", "http:///nohost"):
            with self.assertRaises(ValueError, msg=url):
                shopify_check.assert_public(url)

    def test_allows_public_ip_and_inspect_degrades_to_unknown(self):
        shopify_check.assert_public("https://8.8.8.8/")
        self.assertEqual(shopify_check.inspect_website("http://169.254.169.254/", platform="facebook"), ("unknown", ""))

    def test_redirect_to_internal_is_refused(self):
        h = shopify_check._PublicOnlyRedirects()
        req = shopify_check.urllib.request.Request("https://8.8.8.8/")
        with self.assertRaises(ValueError):
            h.redirect_request(req, None, 302, "Found", {}, "http://169.254.169.254/")


if __name__ == "__main__":
    unittest.main()
