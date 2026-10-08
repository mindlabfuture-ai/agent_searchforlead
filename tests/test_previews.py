import os, sqlite3, unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest import mock

from PIL import Image

from leadagent import db, emailing, imaging, previews, previewui, showcase

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app", "SENDER_FROM_EMAIL": "hello@mail.example.ph",
       "RESEND_API_KEY": "re_test", "EMAIL_DAILY_CAP": "5"}
NOW = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)  # Wed 10:00 PHT


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def png(color, size=(80, 80), fmt="PNG"):
    b = BytesIO(); Image.new("RGB", size, color).save(b, fmt); return b.getvalue()


def contacted_lead(con, days_ago=8, email="owner@glow.ph", name="Glow PH"):
    db.upsert_lead(con, "https://facebook.com/glowph", name, "skincare Manila")
    sent = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")  # naive, like SQLite datetime()
    con.execute("UPDATE leads SET status='contacted', email=? WHERE url LIKE '%glowph'", (email,))
    lead = con.execute("SELECT * FROM leads").fetchone()
    con.execute("INSERT INTO emails (lead_id,to_email,subject,status,sent_at,kind) VALUES (?,?,?,?,?,'initial')",
                (lead["id"], email, "hi", "sent", sent))
    con.commit()
    return lead


@mock.patch.dict(os.environ, ENV)
class ImagingTests(unittest.TestCase):
    def test_rejects_non_images(self):
        with self.assertRaises(ValueError):
            imaging.normalize_upload(b"<svg onload=alert(1)></svg>")
        with self.assertRaises(ValueError):
            imaging.normalize_upload(b"x" * 100)

    def test_reencodes_and_shrinks(self):
        ctype, data = imaging.normalize_upload(png("red", (3000, 1000)))
        self.assertEqual(ctype, "image/jpeg")
        self.assertLessEqual(max(Image.open(BytesIO(data)).size), 1600)

    def test_palette_prefers_product_over_facebook_blue(self):
        r = imaging.analyze_palette([(png("#0F9D9A"), "product"), (png("#1877F2", (400, 400)), "screenshot")])
        self.assertLess(imaging._dist(imaging._rgb(r["brand"]), imaging._rgb("#0F9D9A")), 12)

    def test_palette_ignores_grey_and_white(self):
        self.assertIsNone(imaging.analyze_palette([(png("white"), "product"), (png("#808080"), "product")])["brand"])


@mock.patch.dict(os.environ, ENV)
class PreviewTests(unittest.TestCase):
    def test_contrast_helpers(self):
        self.assertNotEqual(previews.text_on("#ffffff"), "#ffffff")
        self.assertEqual(previews.norm_hex("#8b4513"), "#8B4513")
        self.assertIsNone(previews.norm_hex("javascript:"))

    def test_generate_without_site_uses_samples_and_generated_logo(self):
        con = mem(); lead = contacted_lead(con)
        pv = previews.generate(con, lead, fetch_html=lambda u: None, fetch_image=lambda u: None)
        self.assertEqual(pv["logo"]["kind"], "generated")
        self.assertTrue(pv["products"] and all(p["sample"] for p in pv["products"]))

    def test_uploads_drive_brand_and_survive_rebuild(self):
        con = mem(); lead = contacted_lead(con)
        previews.store_upload(con, lead["id"], "up_p1", "image/png", png("#0F9D9A"))
        pv = previews.generate(con, lead, fetch_html=lambda u: None, fetch_image=lambda u: None)
        self.assertLess(imaging._dist(imaging._rgb(pv["brand"]), imaging._rgb("#0F9D9A")), 12)
        previews.generate(con, lead, fetch_html=lambda u: None, fetch_image=lambda u: None)
        self.assertIn("up_p1", previews.load(con, lead["id"])["uploads"])

    def test_manual_hex_wins(self):
        con = mem(); lead = contacted_lead(con)
        previews.store_upload(con, lead["id"], "up_p1", "image/png", png("#0F9D9A"))
        pv = previews.generate(con, lead, lambda u: None, lambda u: None, overrides={"brand": "#8B4513", "products": []})
        self.assertEqual(pv["brand"], "#8B4513")

    def test_image_tokens(self):
        con = mem(); lead = contacted_lead(con)
        previews.generate(con, lead, lambda u: None, lambda u: None)
        previews.store_upload(con, lead["id"], "shot1", "image/png", png("red"))
        con.execute("INSERT INTO preview_images (lead_id,slot,content_type,data,source_url) VALUES (?,?,?,?,?)",
                    (lead["id"], "p1", "image/png", png("red"), "x"))
        slot = "p1"
        tok = previews.image_token(lead["id"], slot, 1)
        self.assertIsNotNone(previews.image_from_token(con, tok))
        self.assertIsNone(previews.image_from_token(con, tok[:-1] + ("0" if tok[-1] != "0" else "1")))
        self.assertIsNone(previews.image_from_token(con, previews.image_token(lead["id"], "shot1", 1)))  # screenshots are never public

    def test_mockup_escapes_text(self):
        con = mem(); lead = contacted_lead(con, name="<script>alert(1)</script>")
        pv = previews.generate(con, lead, lambda u: None, lambda u: None)
        html = previews.render_mockup(pv, "https://leads.example.app", lead["id"], 1)
        self.assertNotIn("<script>", html)

    def test_theme_settings_is_json_serialisable(self):
        import json
        con = mem(); lead = contacted_lead(con)
        json.dumps(previews.theme_settings(previews.generate(con, lead, lambda u: None, lambda u: None)))


@mock.patch.dict(os.environ, ENV)
class ShowcaseTests(unittest.TestCase):
    def ready(self, **kw):
        con = mem(); lead = contacted_lead(con, **kw)
        previews.generate(con, lead, lambda u: None, lambda u: None)
        return con, lead

    def approve(self, con, lead):
        previewui.apply_action(con, lead["id"], "approve")

    def test_due_only_after_seven_days(self):
        con, lead = self.ready(days_ago=6)
        self.assertFalse(showcase.is_due(con, lead, NOW))
        con, lead = self.ready(days_ago=7)
        self.assertTrue(showcase.is_due(con, lead, NOW))

    def test_needs_approval(self):
        con, lead = self.ready()
        self.assertEqual(showcase.can_send(con, lead, NOW), (False, "preview not approved"))
        self.approve(con, lead)
        self.assertTrue(showcase.can_send(con, lead, NOW)[0])

    def test_blocked_when_suppressed_or_dnc(self):
        con, lead = self.ready(); self.approve(con, lead)
        emailing.suppress(con, lead["email"], "test")
        self.assertFalse(showcase.can_send(con, lead, NOW)[0])
        con, lead = self.ready(); self.approve(con, lead)
        db.add_do_not_contact(con, lead["url"], "x")
        self.assertFalse(showcase.can_send(con, lead, NOW)[0])

    def test_run_sends_once_with_idempotency_key(self):
        con, lead = self.ready(); self.approve(con, lead)
        calls = []
        post = lambda p, k: calls.append((p, k)) or {"id": "re_1"}
        self.assertEqual(showcase.run(con, post=post, now=NOW, enabled=True, log=lambda *_: None), 1)
        self.assertEqual(showcase.run(con, post=post, now=NOW, enabled=True, log=lambda *_: None), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], f"lead-{lead['id']}-showcase")
        self.assertIn("List-Unsubscribe", calls[0][0]["headers"])
        self.assertEqual(previews.load(con, lead["id"])["status"], "sent")

    def test_dry_run_sends_nothing(self):
        con, lead = self.ready(); self.approve(con, lead)
        post = mock.Mock()
        self.assertEqual(showcase.run(con, post=post, now=NOW, enabled=False, log=lambda *_: None), 0)
        post.assert_not_called()

    def test_outside_send_window(self):
        con, lead = self.ready(); self.approve(con, lead)
        sat = datetime(2026, 10, 17, 2, 0, tzinfo=timezone.utc)
        self.assertEqual(showcase.run(con, post=mock.Mock(), now=sat, enabled=True, log=lambda *_: None), 0)

    def test_showcase_does_not_block_or_count_as_initial(self):
        con, lead = self.ready(); self.approve(con, lead)
        showcase.run(con, post=lambda p, k: {"id": "r"}, now=NOW, enabled=True, log=lambda *_: None)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM emails WHERE kind='initial'").fetchone()[0], 1)

    def test_prepare_builds_but_never_approves(self):
        con = mem(); contacted_lead(con, days_ago=5)
        self.assertEqual(showcase.prepare(con, now=NOW, fetch_html=lambda u: None, fetch_image=lambda u: None, log=lambda *_: None), 1)
        self.assertEqual(previews.load(con, 1)["status"], "draft")
        self.assertEqual(showcase.prepare(con, now=NOW, fetch_html=lambda u: None, fetch_image=lambda u: None, log=lambda *_: None), 0)

    def test_email_content(self):
        con, lead = self.ready()
        msg = showcase.build_showcase(lead, previews.load(con, lead["id"]), "https://leads.example.app")
        self.assertIn("Glow PH", msg["subject"])
        self.assertIn("unsubscribe", msg["html"].lower())


@mock.patch.dict(os.environ, ENV)
class UiTests(unittest.TestCase):
    def test_multipart_roundtrip(self):
        b = "XYZ"
        body = (f'--{b}\r\nContent-Disposition: form-data; name="store_name"\r\n\r\nGlow\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="up_p1"; filename="a.png"\r\nContent-Type: image/png\r\n\r\n').encode() \
            + b"\x89PNGDATA" + f'\r\n--{b}\r\nContent-Disposition: form-data; name="up_logo"; filename=""\r\n\r\n\r\n--{b}--\r\n'.encode()
        fields, files = previewui.parse_multipart(f"multipart/form-data; boundary={b}", body)
        self.assertEqual(fields["store_name"], "Glow")
        self.assertEqual(files, {"up_p1": b"\x89PNGDATA"})

    def test_save_rejects_fake_image_and_bad_hex(self):
        con = mem(); lead = contacted_lead(con)
        msg = previewui.save_from_form(con, lead["id"], {"brand": "nope"}, {"up_p1": b"not an image"},
                                       lambda u: None, lambda u: None)
        self.assertIn("not a PNG", msg); self.assertIn("not a colour", msg)
        self.assertNotIn("up_p1", previews.load(con, lead["id"])["uploads"])

    def test_save_uses_uploaded_photo_colour(self):
        con = mem(); lead = contacted_lead(con)
        previewui.save_from_form(con, lead["id"], {}, {"up_p1": png("#0F9D9A"), "shot1": png("#1877F2", (300, 300))},
                                 lambda u: None, lambda u: None)
        self.assertLess(imaging._dist(imaging._rgb(previews.load(con, lead["id"])["brand"]), imaging._rgb("#0F9D9A")), 12)

    def test_approve_requires_email(self):
        con = mem(); lead = contacted_lead(con, email="")
        previews.generate(con, lead, lambda u: None, lambda u: None)
        self.assertIn("email", previewui.apply_action(con, lead["id"], "approve"))

    def test_pages_render_without_error(self):
        con = mem(); lead = contacted_lead(con)
        previews.generate(con, lead, lambda u: None, lambda u: None)
        self.assertIn("Glow PH", previewui.render_list(con, "tok", now=NOW))
        self.assertIn("Glow PH", previewui.render_editor(con, lead["id"], "tok", now=NOW))


if __name__ == "__main__":
    unittest.main()


@mock.patch.dict(os.environ, ENV)
class MockupTileTests(unittest.TestCase):
    def test_product_without_photo_gets_a_visible_tile(self):
        con = mem(); lead = contacted_lead(con)
        pv = previews.generate(con, lead, lambda u: None, lambda u: None, overrides={"brand": "#A7D8D3", "products": []})
        html = previews.render_mockup(pv, "https://leads.example.app", lead["id"], 1)
        soft = previews.mix("#A7D8D3", "#FFFFFF", 0.62)
        self.assertIn(f"background:{soft}", html)
