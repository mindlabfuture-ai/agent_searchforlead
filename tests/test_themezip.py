import csv, io, json, os, sqlite3, unittest, zipfile
from unittest import mock

from PIL import Image

from leadagent import db, previews, themezip

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app"}


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def png(color="#0F9D9A"):
    b = io.BytesIO(); Image.new("RGB", (80, 80), color).save(b, "PNG"); return b.getvalue()


def make(con=None, name="Glow PH", overrides=None, uploads=()):
    con = con or mem()
    db.upsert_lead(con, "https://facebook.com/glowph", name, "skincare Manila")
    lead = con.execute("SELECT * FROM leads").fetchone()
    for slot in uploads:
        previews.store_upload(con, lead["id"], slot, "image/png", png())
    previews.generate(con, lead, lambda u: None, lambda u: None, overrides=overrides)
    return con, lead


def files_for(con, lead_id=1):
    z, csv_text, guide, err = themezip.bundle(con, lead_id, "https://leads.example.app")
    assert err is None, err
    return zipfile.ZipFile(io.BytesIO(z)), csv_text, guide


@mock.patch.dict(os.environ, ENV)
class ThemeTests(unittest.TestCase):
    def test_structure_licence_and_logo(self):
        z, _, _ = files_for(make()[0])
        names = set(z.namelist())
        self.assertTrue({"layout/theme.liquid", "config/settings_data.json", "templates/index.json", "LICENSE.md",
                         "assets/brand-logo.svg", "snippets/brand-logo.liquid"} <= names)
        self.assertTrue(all("/" in n or n == "LICENSE.md" for n in names))  # no wrapping folder, no stray files at the root
        self.assertNotIn("<span class=\"h2\">{{ shop.name }}</span>", z.read("sections/header.liquid").decode())
        self.assertIn("{% render 'brand-logo' %}", z.read("sections/header.liquid").decode())
        self.assertIn("brand-logo.svg", z.read("snippets/brand-logo.liquid").decode())

    def test_uploaded_logo_is_used(self):
        con, lead = make(uploads=("up_logo",))
        con.execute("INSERT OR REPLACE INTO preview_images (lead_id,slot,content_type,data,source_url) VALUES (1,'logo','image/png',?, 'x')", (png(),)); con.commit()
        z, _, _ = files_for(con)
        self.assertIn("assets/brand-logo.png", z.namelist()); self.assertNotIn("assets/brand-logo.svg", z.namelist())

    def test_all_json_is_valid_and_settings_fit_the_schema(self):
        z, _, _ = files_for(make(overrides={"style": "playful", "brand": "#8B4513", "products": []})[0])
        for n in z.namelist():
            if n.endswith(".json") and not n.startswith("locales/"):
                json.loads(z.read(n))
        schema = {}
        for sec in json.loads(z.read("config/settings_schema.json")):
            for st in sec.get("settings", []):
                if "id" in st: schema[st["id"]] = st
        preset = json.loads(z.read("config/settings_data.json"))["presets"]["Dawn"]
        for key in ("buttons_radius", "media_radius", "card_corner_radius", "collection_card_corner_radius", "logo_width"):
            st = schema[key]
            self.assertTrue(st["min"] <= preset[key] <= st["max"] and (preset[key] - st["min"]) % st["step"] == 0, key)
        scheme_keys = {s["id"] for s in schema["color_schemes"]["definition"]} if "color_schemes" in schema else set()
        for sc in preset["color_schemes"].values():
            self.assertTrue(set(sc["settings"]) >= scheme_keys)

    def test_colour_schemes_are_readable_for_any_brand(self):
        for brand in ("#A7D8D3", "#0F9D9A", "#8B4513", "#F5E6A8", "#1A1A40", "#E63946"):
            for accent in (None, "#C9835A"):
                sch = themezip.schemes({"brand": brand, "accent": accent})
                for name, s in sch.items():
                    v = s["settings"]
                    self.assertGreaterEqual(previews.contrast(v["text"], v["background"]), 4.5, (brand, name, "text"))
                    self.assertGreaterEqual(previews.contrast(v["button_label"], v["button"]), 4.5, (brand, name, "button"))
                    self.assertGreaterEqual(previews.contrast(v["secondary_button_label"], v["background"]), 4.5, (brand, name, "outline"))

    def test_home_page_and_style(self):
        con, _ = make(name="Glow & <b>Co</b>", overrides={"style": "elegant", "tagline": "Gentle <skin>"})
        z, _, _ = files_for(con)
        home = json.loads(z.read("templates/index.json"))
        self.assertEqual(home["sections"]["image_banner"]["blocks"]["heading"]["settings"]["heading"], "Welcome to Glow & <b>Co</b>")
        self.assertIn("&lt;skin&gt;", home["sections"]["image_banner"]["blocks"]["text"]["settings"]["text"])
        self.assertEqual(json.loads(z.read("config/settings_data.json"))["presets"]["Dawn"]["type_header_font"], "serif")

    def test_vendored_dawn_is_not_modified_and_patches_fail_loudly(self):
        before = (themezip.BASE / "sections/header.liquid").read_bytes()
        files_for(make()[0])
        self.assertEqual((themezip.BASE / "sections/header.liquid").read_bytes(), before)
        base = themezip.load_base(); base["sections/header.liquid"] = b"nothing to patch"
        with mock.patch.object(themezip, "load_base", return_value=base), self.assertRaises(themezip.ThemeError):
            themezip.build_theme(previews.load(make()[0], 1), {})
        con, _ = make()
        with mock.patch.object(themezip, "load_base", return_value=base):
            self.assertIn("Dawn has changed", themezip.bundle(con, 1, "https://leads.example.app")[3])

    def test_no_preview(self):
        self.assertEqual(themezip.bundle(mem(), 1, "https://x")[3], "Build a preview first.")


@mock.patch.dict(os.environ, ENV)
class ProductsTests(unittest.TestCase):
    def rows(self, con):
        _, csv_text, _ = files_for(con)
        return list(csv.DictReader(io.StringIO(csv_text)))

    def test_samples_are_drafts_tagged_and_priced(self):
        rows = self.rows(make()[0])
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual((r["Status"], r["Published"], r["Tags"]), ("draft", "FALSE", "sample"))
            self.assertRegex(r["Variant Price"], r"^\d+\.\d\d$")
            self.assertEqual(r["Image Src"], "")

    def test_real_products_with_photos_use_signed_links(self):
        con, lead = make(uploads=("up_p1",), overrides={"products": [{"name": "Serum", "price": "₱1,199"}, {"name": "Serum", "price": "bad"}]})
        rows = self.rows(con)
        self.assertEqual([r["Handle"] for r in rows], ["serum", "serum-2"])
        self.assertEqual((rows[0]["Variant Price"], rows[1]["Variant Price"]), ("1199.00", "0.00"))
        self.assertTrue(rows[0]["Image Src"].startswith("https://leads.example.app/i/1.p1."))
        self.assertEqual(rows[0]["Tags"], "")

    def test_price_number(self):
        for text, want in (("₱499", "499.00"), ("PHP 1,299.50", "1299.50"), ("", "0.00"), ("free", "0.00")):
            self.assertEqual(themezip.price_number(text), want)

    def test_csv_cells_cannot_inject_formulas_or_html(self):
        con, _ = make(overrides={"products": [{"name": "<img onerror=1>", "price": "1"}]})
        r = self.rows(con)[0]
        self.assertNotIn("<img", r["Body (HTML)"])

    def test_guide_mentions_samples(self):
        self.assertIn("samples", files_for(make()[0])[2])


if __name__ == "__main__":
    unittest.main()
