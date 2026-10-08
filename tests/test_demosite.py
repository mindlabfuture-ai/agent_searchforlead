import io, os, sqlite3, unittest, zipfile
from datetime import datetime, timedelta, timezone
from unittest import mock

from PIL import Image

from leadagent import db, demosite, emailing, netlify, previews, previewui, showcase

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app", "NETLIFY_AUTH_TOKEN": "nfp_test"}
NOW = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def png(color="#0F9D9A"):
    b = io.BytesIO(); Image.new("RGB", (80, 80), color).save(b, "PNG"); return b.getvalue()


def lead_with_preview(con, name="Glow PH", email="owner@glow.ph"):
    db.upsert_lead(con, "https://facebook.com/glowph", name, "skincare Manila")
    con.execute("UPDATE leads SET status='contacted', email=?, email_source='https://glow.ph'", (email,)); con.commit()
    lead = con.execute("SELECT * FROM leads").fetchone()
    previews.store_upload(con, lead["id"], "up_p1", "image/png", png())
    previews.generate(con, lead, lambda u: None, lambda u: None)
    return lead


class Fake:
    """Records Netlify calls; behaves like the real client for the happy path."""
    def __init__(self, zone=True, fail=()):
        self.calls, self.zone, self.fail = [], zone, set(fail)

    def _do(self, name, *a):
        self.calls.append((name, *a))
        if name in self.fail:
            raise netlify.NetlifyError(f"{name} failed")

    def create_site(self, name): self._do("create_site", name); return {"id": "site1", "default_domain": f"{name}.netlify.app"}
    def deploy_zip(self, sid, data): self._do("deploy_zip", sid, len(data)); return {}
    def set_custom_domain(self, sid, d): self._do("set_custom_domain", sid, d); return {}
    def provision_ssl(self, sid): self._do("provision_ssl", sid); return {}
    def dns_zone(self, d): self._do("dns_zone", d); return {"id": "zone1"} if self.zone else None
    def dns_records(self, z): self._do("dns_records", z); return []
    def create_cname(self, z, h, v): self._do("create_cname", z, h, v); return {"id": "rec1"}
    def delete_dns_record(self, z, r): self._do("delete_dns_record", z, r); return {}
    def delete_site(self, sid): self._do("delete_site", sid); return {}


@mock.patch.dict(os.environ, ENV)
class SiteTests(unittest.TestCase):
    def test_site_files_and_guardrails(self):
        con = mem(); lead = lead_with_preview(con, name='<script>x</script> Glow')
        files = zipfile.ZipFile(io.BytesIO(demosite.site_zip(con, lead["id"]))).namelist()
        self.assertTrue({"index.html", "style.css", "app.js", "robots.txt", "_headers", "img/p1.jpg"} <= set(files) or "img/p1.png" in files)
        z = zipfile.ZipFile(io.BytesIO(demosite.site_zip(con, lead["id"])))
        page = z.read("index.html").decode()
        self.assertIn("noindex", page); self.assertIn("not a live store", page)
        self.assertNotIn("<script>x", page)
        self.assertNotRegex(page, r"<script(?![^>]*src=)"); self.assertNotIn(" style=", page); self.assertNotIn(" onerror", page)
        self.assertIn("Disallow: /", z.read("robots.txt").decode())
        self.assertIn("X-Robots-Tag", z.read("_headers").decode())

    def test_sample_products_are_labelled(self):
        con = mem()
        db.upsert_lead(con, "https://facebook.com/g", "Glow", "skincare"); lead = con.execute("SELECT * FROM leads").fetchone()
        previews.generate(con, lead, lambda u: None, lambda u: None)
        page = zipfile.ZipFile(io.BytesIO(demosite.site_zip(con, 1))).read("index.html").decode()
        self.assertIn("(sample)", page); self.assertIn("Sample products shown", page)

    def test_slug_is_the_business_name_and_unique(self):
        self.assertEqual(previews.domain_slug("Glow PH Skin Co."), "glow-ph-skin-co")
        self.assertEqual(previews.domain_slug("!!!"), "yourshop")
        con = mem(); lead = lead_with_preview(con)
        con.execute("INSERT INTO demo_sites (lead_id,slug,status) VALUES (99,'glow-ph','live')"); con.commit()
        self.assertEqual(demosite.pick_slug(con, lead["id"], "Glow PH"), "glow-ph-2")


@mock.patch.dict(os.environ, ENV)
class PublishTests(unittest.TestCase):
    def test_publish_creates_site_domain_and_dns(self):
        con = mem(); lead = lead_with_preview(con); fake = Fake()
        ok, msg = demosite.publish(con, lead["id"], fake, NOW)
        self.assertTrue(ok, msg)
        names = [c[0] for c in fake.calls]
        self.assertEqual(names[:3], ["create_site", "deploy_zip", "set_custom_domain"])
        self.assertIn(("create_cname", "zone1", "glow-ph.mindlabfuture-ai.com", "mlf-demo-glow-ph.netlify.app"), fake.calls)
        d = demosite.get(con, lead["id"])
        self.assertEqual((d["status"], d["url"]), ("live", "https://glow-ph.mindlabfuture-ai.com"))
        self.assertEqual(d["expires_at"][:10], "2026-11-13")

    def test_republish_updates_the_same_site(self):
        con = mem(); lead = lead_with_preview(con); fake = Fake()
        demosite.publish(con, lead["id"], fake, NOW); fake.calls.clear()
        self.assertTrue(demosite.publish(con, lead["id"], fake, NOW)[0])
        self.assertEqual([c[0] for c in fake.calls], ["deploy_zip"])

    def test_custom_domain_failure_falls_back_to_netlify_address(self):
        con = mem(); lead = lead_with_preview(con)
        ok, msg = demosite.publish(con, lead["id"], Fake(fail={"set_custom_domain"}), NOW)
        d = demosite.get(con, lead["id"])
        self.assertTrue(ok); self.assertEqual(d["url"], "https://mlf-demo-glow-ph.netlify.app"); self.assertIn("custom address failed", d["note"])

    def test_total_failure_stores_nothing(self):
        con = mem(); lead = lead_with_preview(con)
        self.assertFalse(demosite.publish(con, lead["id"], Fake(fail={"create_site"}), NOW)[0])
        self.assertIsNone(demosite.get(con, lead["id"]))

    def test_refuses_for_opted_out_or_missing_preview_or_token(self):
        con = mem(); lead = lead_with_preview(con); emailing.suppress(con, "owner@glow.ph", "x")
        self.assertFalse(demosite.publish(con, lead["id"], Fake(), NOW)[0])
        con = mem(); lead = lead_with_preview(con); db.add_do_not_contact(con, lead["url"], "x")
        self.assertFalse(demosite.publish(con, lead["id"], Fake(), NOW)[0])
        con = mem(); db.upsert_lead(con, "https://facebook.com/x", "X", "")
        self.assertIn("preview", demosite.publish(con, 1, Fake(), NOW)[1])
        con = mem(); lead = lead_with_preview(con)
        with mock.patch.dict(os.environ, {"NETLIFY_AUTH_TOKEN": ""}):
            self.assertIn("NETLIFY_AUTH_TOKEN", demosite.publish(con, lead["id"], None, NOW)[1])


@mock.patch.dict(os.environ, ENV)
class TakedownTests(unittest.TestCase):
    def live(self):
        con = mem(); lead = lead_with_preview(con); fake = Fake()
        demosite.publish(con, lead["id"], fake, NOW); fake.calls.clear()
        return con, lead, fake

    def test_unpublish_removes_dns_and_site(self):
        con, lead, fake = self.live()
        self.assertTrue(demosite.unpublish(con, lead["id"], fake)[0])
        self.assertEqual([c[0] for c in fake.calls], ["delete_dns_record", "delete_site"])
        self.assertEqual(demosite.get(con, lead["id"])["status"], "deleted")

    def test_failed_delete_stays_live_to_retry(self):
        con, lead, fake = self.live(); fake.fail = {"delete_site"}
        self.assertFalse(demosite.unpublish(con, lead["id"], fake)[0])
        self.assertEqual(demosite.get(con, lead["id"])["status"], "live")

    def test_cleanup_expired(self):
        con, lead, fake = self.live()
        self.assertEqual(demosite.cleanup(con, fake, NOW + timedelta(days=29), log=lambda *_: None), 0)
        self.assertEqual(demosite.cleanup(con, fake, NOW + timedelta(days=31), log=lambda *_: None), 1)

    def test_cleanup_on_opt_out_in_every_form(self):
        for how in ("suppress", "dnc", "lost", "skipped"):
            con, lead, fake = self.live()
            if how == "suppress": emailing.suppress(con, "owner@glow.ph", "unsubscribed")
            if how == "dnc": db.add_do_not_contact(con, lead["url"], "x")
            if how == "lost": con.execute("UPDATE leads SET status='lost'"); con.commit()
            if how == "skipped": con.execute("UPDATE previews SET status='skipped'"); con.commit()
            self.assertEqual(demosite.cleanup(con, fake, NOW, log=lambda *_: None), 1, how)

    def test_dnc_button_takes_the_site_down(self):
        con, lead, fake = self.live()
        previewui.apply_action(con, lead["id"], "dnc", fake)
        self.assertEqual(demosite.get(con, lead["id"])["status"], "deleted")

    def test_extend(self):
        con, lead, fake = self.live()
        demosite.extend(con, lead["id"], NOW + timedelta(days=20))
        self.assertEqual(demosite.get(con, lead["id"])["expires_at"][:10], "2026-12-03")


@mock.patch.dict(os.environ, ENV)
class EmailAndClientTests(unittest.TestCase):
    def test_showcase_links_the_demo_only_while_live(self):
        con = mem(); lead = lead_with_preview(con)
        pv = previews.load(con, lead["id"])
        plain = showcase.build_showcase(lead, pv, "https://leads.example.app")
        self.assertNotIn("click through", plain["html"].lower())
        live = showcase.build_showcase(lead, pv, "https://leads.example.app", "https://glow-ph.mindlabfuture-ai.com")
        self.assertIn("glow-ph.mindlabfuture-ai.com", live["html"]); self.assertIn("https://glow-ph.mindlabfuture-ai.com", live["text"])

    def test_client_calls_the_right_endpoints(self):
        seen = []
        c = netlify.Client("tok", "my-team", request=lambda m, u, t, b, ct: seen.append((m, u.replace(netlify.API, ""), ct)) or {})
        c.create_site("a"); c.deploy_zip("s", b"zip"); c.set_custom_domain("s", "a.example.com"); c.delete_site("s")
        self.assertEqual(seen, [("POST", "/my-team/sites", "application/json"), ("POST", "/sites/s/deploys", "application/zip"),
                                ("PATCH", "/sites/s", "application/json"), ("DELETE", "/sites/s", "application/json")])
        with self.assertRaises(netlify.NetlifyError):
            netlify.Client("")


if __name__ == "__main__":
    unittest.main()
