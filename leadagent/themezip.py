"""A Shopify theme ZIP for a lead's preview: Shopify's Dawn (v16.0.0, vendored in theme_base/) with the store's
colours, logo, home page text and button shape applied, plus a product import CSV and a setup guide.

Dawn's licence limits it to themes that work with Shopify and requires the notice to stay with the code; this does
both (the notice ships inside the zip). The changes are made on a copy, never to the vendored files, and each
text patch fails loudly if Dawn's markup is not what it expects, so a future Dawn upgrade cannot silently break it."""
import csv
import html
import io
import json
import re
import zipfile
from pathlib import Path

from . import previews

BASE = Path(__file__).parent / "theme_base" / "dawn"
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
SHAPE = {"modern": {"buttons": 40, "media": 12, "card": 12}, "elegant": {"buttons": 0, "media": 0, "card": 0},
         "playful": {"buttons": 40, "media": 20, "card": 20}}
LOGO_WIDTH = 160  # Dawn's logo_width: 50 to 300 in steps of 10


class ThemeError(Exception):
    pass


def load_base():
    """{path: bytes} for every file of the vendored Dawn except its developer dotfiles (.gitignore and friends)."""
    if not BASE.is_dir():
        raise ThemeError("the Dawn base theme is missing from theme_base/")
    return {p.relative_to(BASE).as_posix(): p.read_bytes() for p in sorted(BASE.rglob("*"))
            if p.is_file() and not p.name.startswith(".")}


def schemes(profile):
    """Dawn's five colour schemes from the preview palette. Text colours come from previews.text_on, so they read."""
    pal = previews.palette(profile["brand"], profile.get("accent"))
    brand, accent, ink = pal["brand"], pal["accent"], pal["ink"]
    brand_ink = brand
    for k in range(1, 9):  # brand colour used as button text or outline: darken until it reads on white and on the tint
        if min(previews.contrast(brand_ink, "#FFFFFF"), previews.contrast(brand_ink, pal["tint"])) >= 4.5:
            break
        brand_ink = previews.mix(brand, "#000000", k * 0.12)
    s = lambda bg, text, button, label, secondary: {"settings": {"background": bg, "background_gradient": "", "text": text, "button": button,
                                                                  "button_label": label, "secondary_button_label": secondary, "shadow": ink}}
    return {
        "scheme-1": s("#FFFFFF", ink, brand, pal["on_brand"], brand_ink),
        "scheme-2": s(pal["tint"], ink, brand, pal["on_brand"], brand_ink),
        "scheme-3": s(brand, pal["on_brand"], pal["on_brand"], brand, pal["on_brand"]),
        "scheme-4": s(ink, "#FFFFFF", accent, pal["on_accent"], "#FFFFFF"),
        "scheme-5": s(accent, pal["on_accent"], pal["on_accent"], accent, pal["on_accent"]),
    }


def _replace(text, old, new, count, where):
    if text.count(old) != count:
        raise ThemeError(f"Dawn has changed: expected {count} x {old[:50]!r} in {where}")
    return text.replace(old, new)


def logo_files(profile, images):
    """(asset file name, bytes, kind): the uploaded or fetched logo, else the generated placeholder as SVG."""
    if "logo" in images and images["logo"][0] in EXT:
        return f"brand-logo.{EXT[images['logo'][0]]}", images["logo"][1], "image"
    return "brand-logo.svg", previews.generated_logo_svg(profile["name"], profile["brand"]).encode(), "generated"


def build_theme(profile, images):
    """{path: bytes} for the theme zip. `images` maps slot (logo, p1..p3) to (content_type, data)."""
    files = load_base()
    name = profile["name"]
    style = profile.get("style") if profile.get("style") in SHAPE else "modern"
    shape = SHAPE[style]

    # colours, shape, logo size: the Dawn preset the theme starts from
    data = json.loads(files["config/settings_data.json"])
    preset = data["presets"]["Dawn"]
    preset["color_schemes"] = schemes(profile)
    preset.update({"buttons_radius": shape["buttons"], "media_radius": shape["media"], "card_corner_radius": shape["card"],
                   "collection_card_corner_radius": shape["card"], "logo_width": LOGO_WIDTH})
    if style == "elegant":
        preset["type_header_font"] = "serif"  # a system serif: always a valid font handle
    files["config/settings_data.json"] = (json.dumps(data, indent=2) + "\n").encode()

    # logo: an image_picker cannot point at a file inside the zip, so the header shows it as a theme asset
    asset, blob, kind = logo_files(profile, images)
    files[f"assets/{asset}"] = blob
    files["snippets/brand-logo.liquid"] = (
        f'<img class="header__heading-logo motion-reduce" src="{{{{ \'{asset}\' | asset_url }}}}" alt="{{{{ shop.name | escape }}}}" '
        f'width="{{{{ settings.logo_width }}}}" height="auto" loading="eager">\n').encode()
    header = files["sections/header.liquid"].decode()
    header = _replace(header, '<span class="h2">{{ shop.name }}</span>', "{% render 'brand-logo' %}", 2, "sections/header.liquid")
    files["sections/header.liquid"] = header.encode()

    # home page: the store's own words
    tagline = profile.get("tagline") or "Order online and pay by GCash, Maya or bank transfer."
    home = json.loads(files["templates/index.json"])
    banner = home["sections"]["image_banner"]
    banner["blocks"]["heading"]["settings"]["heading"] = f"Welcome to {name}"
    banner["blocks"]["text"] = {"type": "text", "settings": {"text": f"<p>{html.escape(tagline)}</p>", "text_style": "body"}}
    banner["block_order"] = ["heading", "text", "button"]
    banner["blocks"]["button"]["settings"]["button_label_1"] = "Shop now"
    banner["blocks"]["button"]["settings"]["button_style_secondary_1"] = False
    home["sections"]["featured_collection"]["settings"]["title"] = "Our products"
    files["templates/index.json"] = (json.dumps(home, indent=2) + "\n").encode()
    return files


def theme_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            z.writestr(zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0)), files[path])
    return buf.getvalue()


# ---------------- products ----------------
COLUMNS = ["Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published", "Option1 Name", "Option1 Value", "Variant Price",
           "Variant Inventory Policy", "Variant Fulfillment Service", "Variant Requires Shipping", "Variant Taxable", "Image Src",
           "Image Position", "Image Alt Text", "Status"]


def price_number(text):
    """'₱1,199' -> '1199.00'; '' or unreadable -> '0.00' (the owner fills it in)."""
    m = re.search(r"\d[\d,]*(?:\.\d+)?", text or "")
    return f"{float(m.group(0).replace(',', '')):.2f}" if m else "0.00"


def products_csv(profile, base_url, lead_id, version, images):
    """Shopify's product import format. Rows are drafts, so nothing goes live until the owner reviews them. Image
    addresses are the signed, unlisted links the importer can fetch. Sample products are tagged 'sample'."""
    out = io.StringIO()
    w = csv.DictWriter(out, COLUMNS, lineterminator="\n")
    w.writeheader()
    used = set()
    for n, p in enumerate(profile["products"], 1):
        handle = previews.domain_slug(p["name"])
        base, k = handle, 2
        while handle in used:
            handle, k = f"{base}-{k}", k + 1
        used.add(handle)
        slot = f"p{p.get('slot', n)}"
        has_img = p.get("has_image") and slot in images
        w.writerow({"Handle": handle, "Title": p["name"], "Body (HTML)": f"<p>{html.escape(p['name'])}</p>", "Vendor": profile["name"],
                    "Type": profile.get("niche", ""), "Tags": "sample" if p["sample"] else "", "Published": "FALSE",
                    "Option1 Name": "Title", "Option1 Value": "Default Title", "Variant Price": price_number(p["price"]),
                    "Variant Inventory Policy": "deny", "Variant Fulfillment Service": "manual", "Variant Requires Shipping": "TRUE",
                    "Variant Taxable": "TRUE", "Image Src": previews.image_url(base_url, lead_id, slot, version) if has_img else "",
                    "Image Position": 1 if has_img else "", "Image Alt Text": p["name"] if has_img else "", "Status": "draft"})
    return out.getvalue()


def setup_guide(profile, has_uploaded_logo):
    name = profile["name"]
    sample = any(p["sample"] for p in profile["products"])
    return f"""# Setting up the {name} store

Built from the preview you sent them. Brand colour {profile['brand']}, {profile.get('style', 'modern')} style.
Theme: Shopify Dawn 16.0.0 with the colours, logo and home page text applied.

## 1. Upload the theme
Online Store > Themes > Add theme > Upload zip file > choose `theme.zip`. Then Customize to check it, and Publish when it is ready.

## 2. Add the products
Products > Import > choose `products.csv`. They are imported as drafts so nothing goes live by accident.
{"The products are samples (tagged 'sample'): replace the names, prices and photos with the real ones before publishing." if sample else "Check the names and prices, then set each product to Active."}
Prices that could not be read are 0.00: fix them first.

## 3. Logo
{"The theme shows their logo from `assets/brand-logo` in the header. To use a different file, replace that asset in the theme code editor." if has_uploaded_logo else "No logo was supplied, so the theme shows a simple generated placeholder (their initials and name). Replace the file `assets/brand-logo.svg` in the theme code editor with the real logo, or ask them for one."}

## 4. Payments and POPLoad
Settings > Payments: add the manual payment methods they use (bank transfer, GCash, Maya).
Install POPLoad so customers can upload their receipt and the owner can approve it in one click. Test the install on a client transfer store first.

## 5. Handover
Create the store as a client transfer store from your Partner Dashboard, check everything above, then transfer it. Ask for collaborator access afterwards.

The domain is not included. They can use a free `yourshop.mindlabfuture-ai.com` subdomain or buy their own.
Dawn's licence: keep `LICENSE.md` in the theme; the theme must be used with Shopify.
"""


def bundle(con, lead_id, base_url):
    """(theme_zip_bytes, products_csv, setup_md, None) or (None, None, None, reason)."""
    pv = previews.load(con, lead_id)
    if not pv:
        return None, None, None, "Build a preview first."
    rows = con.execute("SELECT slot, content_type, data FROM preview_images WHERE lead_id=? AND slot IN ('logo','p1','p2','p3')", (lead_id,)).fetchall()
    images = {r["slot"]: (r["content_type"], r["data"]) for r in rows}
    version = re.sub(r"\D", "", pv["updated_at"] or "")[:14] or "0"
    try:
        zbytes = theme_zip(build_theme(pv, images))
    except ThemeError as e:
        return None, None, None, str(e)
    return zbytes, products_csv(pv, base_url, lead_id, version, images), setup_guide(pv, "logo" in images), None
