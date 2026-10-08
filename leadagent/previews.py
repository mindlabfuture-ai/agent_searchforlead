"""Store previews: turn what is publicly known about a lead into a themed mockup of the Shopify store we would
build for them: their brand colour, their logo (or a generated one), and one to three products.

Where the information comes from, and what is never used:
  * the lead's OWN website, if they have one: theme colour, logo, JSON-LD products. These are public pages the
    business published itself.
  * anything you paste in by hand (logo URL, brand colour, products), for example the image address copied from
    their Facebook or Instagram page.
  * never scraped from Facebook or Instagram. Those platforms' terms forbid it and nothing here logs in or crawls.
When nothing is available the logo is generated (initials on the brand colour) and the products are clearly
labelled samples. The result is only ever sent to that one business, as a preview of their own store."""
import colorsys
import hashlib
import hmac
import html as H
import json
import re
from urllib.parse import urljoin, urlparse

from . import config, db, emailing, imaging, shopify_check

E = lambda v: H.escape(str(v), quote=True)
HEX6 = re.compile(r"^#[0-9a-fA-F]{6}$")
SLOTS = ("logo", "p1", "p2", "p3")                      # the images used in the email (served by signed URL)
UPLOAD_SLOTS = ("up_logo", "up_p1", "up_p2", "up_p3", "shot1", "shot2", "shot3")  # what you uploaded (private)
STYLES = {  # fonts and button shape for the mockup
    "modern": {"head": "Helvetica,Arial,sans-serif", "radius": 999, "label": "Modern and clean"},
    "elegant": {"head": "Georgia,'Times New Roman',serif", "radius": 4, "label": "Elegant (serif)"},
    "playful": {"head": "'Trebuchet MS',Helvetica,Arial,sans-serif", "radius": 14, "label": "Friendly and playful"},
}
MAX_PRODUCTS = 3

# Sample products by niche, used only when the business has none we can legitimately read. Always labelled "sample".
SAMPLES = {
    "skincare": ("Glow Serum", "Daily Moisturizer", "Gentle Cleanser"), "beauty": ("Glow Serum", "Daily Moisturizer", "Lip Tint"),
    "cosmetics": ("Matte Lipstick", "Cushion Foundation", "Brow Pencil"), "perfume": ("Signature Eau de Parfum", "Travel Mist", "Gift Set"),
    "clothing": ("Everyday Tee", "Relaxed Pants", "Summer Dress"), "apparel": ("Everyday Tee", "Relaxed Pants", "Summer Dress"),
    "activewear": ("Training Leggings", "Sports Bra", "Performance Tee"), "shoes": ("Classic Sneakers", "Comfort Sandals", "Casual Loafers"),
    "bags": ("Everyday Tote", "Crossbody Bag", "Mini Backpack"), "jewelry": ("Dainty Necklace", "Hoop Earrings", "Stacking Ring"),
    "accessories": ("Silk Scarf", "Leather Belt", "Hair Clips Set"), "watches": ("Classic Watch", "Leather Strap", "Watch Case"),
    "phone accessories": ("Phone Case", "Fast Charger", "Wireless Earbuds"), "gadgets": ("Smart Speaker", "Power Bank", "Desk Lamp"),
    "home decor": ("Ceramic Vase", "Wall Art Print", "Throw Pillow"), "candles": ("Soy Candle", "Reed Diffuser", "Gift Set"),
    "food": ("House Special", "Family Pack", "Gift Box"), "snacks": ("Crunchy Mix", "Sweet Treats Box", "Party Pack"),
    "coffee": ("House Blend 250g", "Cold Brew Pack", "Coffee Gift Set"), "pet supplies": ("Chew Toy", "Pet Bed", "Treat Pouch"),
    "baby products": ("Soft Onesie Set", "Swaddle Blanket", "Baby Wash"), "plants": ("Snake Plant", "Ceramic Pot", "Starter Kit"),
    "handmade crafts": ("Handmade Basket", "Woven Coaster Set", "Craft Gift Box"), "thrift": ("Vintage Jacket", "Denim Pieces", "Mystery Bundle"),
}
GENERIC_SAMPLES = ("Best Seller", "New Arrival", "Gift Set")
SAMPLE_PRICES = ("₱499", "₱799", "₱1,199")
LUXURY = {"watches", "jewelry", "perfume", "bags"}  # these get a serif look


# ---------------- colours ----------------
def _rgb(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(r, g, b):
    return "#{:02X}{:02X}{:02X}".format(*(max(0, min(255, round(v))) for v in (r, g, b)))


def luminance(h):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def text_on(bg):
    """White or near-black, whichever reads better on `bg`."""
    return "#FFFFFF" if contrast(bg, "#FFFFFF") >= contrast(bg, "#111111") else "#111111"


def mix(h, other, amount):
    a, b = _rgb(h), _rgb(other)
    return _hex(*(x + (y - x) * amount for x, y in zip(a, b)))


def _is_boring(h):
    r, g, b = (v / 255 for v in _rgb(h))
    _, l, s = colorsys.rgb_to_hls(r, g, b)
    return s < 0.18 or l > 0.93 or l < 0.08


def norm_hex(value):
    v = (value or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", v):
        v = "#" + "".join(c * 2 for c in v[1:])
    return v.upper() if HEX6.match(v) else None


def seeded_color(seed):
    """A pleasant, stable brand colour for a business we know nothing about, from its name."""
    n = int(hashlib.sha256(seed.lower().encode()).hexdigest()[:8], 16)
    r, g, b = colorsys.hls_to_rgb((n % 360) / 360, 0.42, 0.55)
    return _hex(r * 255, g * 255, b * 255)


def pick_brand_color(override=None, theme_color=None, site_colors=(), seed=""):
    c = norm_hex(override)
    if c:  # an explicit choice by the owner is always respected
        return c, "the colour you chose"
    c = norm_hex(theme_color)
    if c and not _is_boring(c):
        return c, "your website's theme colour"
    for c in site_colors:
        c = norm_hex(c)
        if c and not _is_boring(c):
            return c, "the main colour on your website"
    return seeded_color(seed or "shop"), "a colour chosen for you (send me yours)"


def palette(brand, accent=None):
    return {"brand": brand, "accent": accent or brand, "on_accent": text_on(accent or brand), "on_brand": text_on(brand), "dark": mix(brand, "#000000", 0.35), "tint": mix(brand, "#FFFFFF", 0.88),
            "ink": "#1B1B1F", "muted": "#6B6F76", "line": "#E4E6EB"}


# ---------------- reading a lead's own website ----------------
def _meta(html, *names):
    for n in names:
        m = (re.search(r'<meta[^>]+(?:name|property)=["\']%s["\'][^>]*content=["\']([^"\']*)["\']' % re.escape(n), html, re.I)
             or re.search(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:name|property)=["\']%s["\']' % re.escape(n), html, re.I))
        if m:
            return H.unescape(m.group(1)).strip()
    return ""


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _first(v):
    if isinstance(v, list):
        return _first(v[0]) if v else ""
    if isinstance(v, dict):
        return v.get("url") or v.get("contentUrl") or ""
    return v or ""


def parse_site(html, base_url):
    """Brand clues from the lead's own homepage: no network, pure parsing."""
    html = html or ""
    logos = []
    for m in re.finditer(r"<img\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(r"logo", tag, re.I):
            src = re.search(r'\bsrc=["\']([^"\']+)["\']', tag, re.I)
            if src:
                logos.append(src.group(1))
    for m in re.finditer(r"<link\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(r'rel=["\'][^"\']*(apple-touch-icon|icon)', tag, re.I):
            href = re.search(r'\bhref=["\']([^"\']+)["\']', tag, re.I)
            if href and not href.group(1).lower().split("?")[0].endswith((".ico", ".svg")):
                logos.append(href.group(1))
    og = _meta(html, "og:image")
    if og:
        logos.append(og)
    logos = [u for u in dict.fromkeys(urljoin(base_url, u) for u in logos) if urlparse(u).scheme in ("http", "https")]
    counts = {}
    for c in re.findall(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b", html[:300_000]):
        c = norm_hex(c)
        if c and not _is_boring(c):
            counts[c] = counts.get(c, 0) + 1
    products = []
    for block in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        for node in _walk(data):
            kind = node.get("@type")
            if "Product" in (kind if isinstance(kind, list) else [kind]) and node.get("name"):
                offers = node.get("offers")
                offer = offers[0] if isinstance(offers, list) and offers else offers if isinstance(offers, dict) else {}
                price = str(offer.get("price", "")).strip()
                cur = str(offer.get("priceCurrency", "")).upper()
                shown = (f"₱{price}" if cur == "PHP" else f"{price} {cur}".strip()) if price else ""
                img = _first(node.get("image"))
                products.append({"name": str(node["name"])[:60], "price": shown[:20],
                                 "image_url": urljoin(base_url, img) if img else ""})
            if len(products) >= MAX_PRODUCTS:
                break
    name = _meta(html, "og:site_name") or re.sub(r"\s*[|\-–—].*$", "", (re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S) or [None, ""])[1]).strip()
    return {"site_name": H.unescape(name)[:80], "theme_color": _meta(html, "theme-color"), "colors": sorted(counts, key=counts.get, reverse=True)[:5],
            "logo_urls": logos[:3], "products": products[:MAX_PRODUCTS]}


def detect_niche(*texts):
    blob = " ".join(t or "" for t in texts).lower()
    for niche in sorted(SAMPLES, key=len, reverse=True):  # "phone accessories" before "accessories"
        if niche in blob:
            return niche
    return None


def slugify(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())[:20] or "yourshop"


# ---------------- building a preview ----------------
def _clean(text, limit):
    return " ".join(str(text or "").split())[:limit]


def build_profile(lead, existing=None, fetch_html=None, fetch_image=None, uploads=None):
    """Returns (profile, images). `existing` carries the owner's overrides (details form), `uploads` maps the
    UPLOAD_SLOTS to (content_type, bytes). Network access is injected so this is testable."""
    fetch_html = fetch_html or (lambda url: shopify_check.fetch(url)[0])
    fetch_image = fetch_image or shopify_check.fetch_image
    ov = dict((existing or {}).get("overrides", {}))
    uploads = uploads or {}
    name = _clean(ov.get("store_name") or lead["name"], 60) or "Your Shop"
    notes, images = [], {}
    site = {"site_name": "", "theme_color": "", "colors": [], "logo_urls": [], "products": []}
    website = (lead["website"] or "").strip()
    if website and not any(m in website.lower() for m in shopify_check.MARKETPLACES):
        url = website if re.match(r"https?://", website) else "https://" + website
        try:
            site = parse_site(fetch_html(url), url)
            notes.append("read the business's own website")
        except Exception as e:
            notes.append(f"could not read the website ({str(e)[:60]})")
    niche = ov.get("niche") if ov.get("niche") in SAMPLES else detect_niche(lead["name"], lead["snippet"])

    # logo: your upload first, then a URL you pasted, then the website's, else a generated one
    logo = {"kind": "generated", "source": ""}
    if "up_logo" in uploads:
        ctype, data = uploads["up_logo"]
        images["logo"] = (ctype, data, "uploaded by you")
        logo = {"kind": "image", "source": "uploaded by you"}
    else:
        for url in ([ov["logo_url"]] if ov.get("logo_url") else []) + site["logo_urls"]:
            try:
                data, ctype = fetch_image(url)
            except Exception:
                continue
            images["logo"] = (ctype, data, url)
            logo = {"kind": "image", "source": url}
            break

    # products: slots 1-3 from your details and uploads, else the website's, else clearly labelled samples
    given = ov.get("products") or []
    wanted = []
    for i in range(1, MAX_PRODUCTS + 1):
        o = given[i - 1] if i - 1 < len(given) and isinstance(given[i - 1], dict) else {}
        if o.get("name") or o.get("image_url") or f"up_p{i}" in uploads:
            wanted.append(dict(o, slot=i))
    if not wanted:
        wanted = [dict(p, slot=i) for i, p in enumerate(site["products"][:MAX_PRODUCTS], 1)]
    products = []
    for p in wanted:
        i = p["slot"]
        pname = _clean(p.get("name"), 60) or (f"Product {i}" if f"up_p{i}" in uploads else "")
        if not pname:
            continue
        item = {"slot": i, "name": pname, "price": _clean(p.get("price"), 20), "sample": False, "has_image": False}
        if f"up_p{i}" in uploads:
            ctype, data = uploads[f"up_p{i}"]
            images[f"p{i}"] = (ctype, data, "uploaded by you"); item["has_image"] = True
        elif p.get("image_url"):
            try:
                data, ctype = fetch_image(p["image_url"])
                images[f"p{i}"] = (ctype, data, p["image_url"]); item["has_image"] = True
            except Exception:
                pass
        products.append(item)
    if not products:
        names = SAMPLES.get(niche, GENERIC_SAMPLES)
        products = [{"slot": i + 1, "name": n, "price": SAMPLE_PRICES[i], "sample": True, "has_image": False} for i, n in enumerate(names[:MAX_PRODUCTS])]
        notes.append("used sample products")

    # palette: what the agent concludes from the pictures
    def analyse(sources):
        try:
            return imaging.analyze_palette(sources) if sources else None
        except Exception:
            return None
    shots = [(uploads[k][1], "screenshot") for k in ("shot1", "shot2", "shot3") if k in uploads]
    mine = [(images[k][1], "logo" if k == "logo" else "product") for k in images if images[k][2] == "uploaded by you"] + shots
    auto = [(images[k][1], "logo" if k == "logo" else "product") for k in images]
    mine_a, auto_a = analyse(mine), analyse(auto)
    manual = norm_hex(ov.get("brand"))
    theme = norm_hex(site["theme_color"])
    brand = accent = None
    if manual:
        brand, brand_note = manual, "the colour you chose"
    elif mine_a and mine_a["brand"]:
        brand, accent, brand_note = mine_a["brand"], mine_a["accent"], f"chosen from your {mine_a['basis']}"
    elif theme and not _is_boring(theme):
        brand, brand_note = theme, "your website's theme colour"
    elif auto_a and auto_a["brand"]:
        brand, accent, brand_note = auto_a["brand"], auto_a["accent"], f"chosen from your {auto_a['basis']}"
    else:
        brand, brand_note = pick_brand_color(None, None, site["colors"], name)
    accent = norm_hex(ov.get("accent")) or accent
    swatches = (mine_a or auto_a or {}).get("colors") or [brand]
    style = ov.get("style") if ov.get("style") in STYLES else ("elegant" if niche in LUXURY else "modern")
    profile = {"name": name, "slug": slugify(name), "niche": niche, "brand": brand, "accent": accent, "brand_note": brand_note,
              "swatches": swatches, "style": style, "tagline": _clean(ov.get("tagline"), 120), "logo": logo, "products": products,
              "site": website, "notes": notes, "overrides": ov}
    return profile, images


def store_upload(con, lead_id, slot, ctype, data, source="uploaded by you"):
    if slot not in UPLOAD_SLOTS:
        raise ValueError("unknown upload slot")
    con.execute("INSERT OR REPLACE INTO preview_images (lead_id,slot,content_type,data,source_url) VALUES (?,?,?,?,?)",
                (lead_id, slot, ctype, data, source))
    con.commit()


def clear_upload(con, lead_id, slot):
    if slot in UPLOAD_SLOTS:
        con.execute("DELETE FROM preview_images WHERE lead_id=? AND slot=?", (lead_id, slot))
        con.commit()


def get_uploads(con, lead_id):
    return {r["slot"]: (r["content_type"], r["data"]) for r in
            con.execute("SELECT slot, content_type, data FROM preview_images WHERE lead_id=? AND slot IN (%s)" % ",".join("?" * len(UPLOAD_SLOTS)),
                        (lead_id, *UPLOAD_SLOTS))}


def save(con, lead_id, profile, images, status=None):
    now = db.now()
    old = con.execute("SELECT status FROM previews WHERE lead_id=?", (lead_id,)).fetchone()
    status = status or (old["status"] if old and old["status"] != "sent" else "draft")
    con.execute("INSERT INTO previews (lead_id,profile,status,created_at,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(lead_id) DO UPDATE SET profile=excluded.profile, status=?, updated_at=excluded.updated_at",
                (lead_id, json.dumps(profile), status, now, now, status))
    # only the images used in the email are rewritten: your uploads (up_*, shot*) are kept across rebuilds
    con.execute("DELETE FROM preview_images WHERE lead_id=? AND slot IN ('logo','p1','p2','p3')", (lead_id,))
    for slot, (ctype, data, src) in images.items():
        con.execute("INSERT INTO preview_images (lead_id,slot,content_type,data,source_url) VALUES (?,?,?,?,?)", (lead_id, slot, ctype, data, src))
    con.commit()


def load(con, lead_id):
    row = con.execute("SELECT * FROM previews WHERE lead_id=?", (lead_id,)).fetchone()
    if not row:
        return None
    p = json.loads(row["profile"])
    p["status"], p["updated_at"], p["approved_at"] = row["status"], row["updated_at"], row["approved_at"]
    slots = [r["slot"] for r in con.execute("SELECT slot FROM preview_images WHERE lead_id=?", (lead_id,))]
    p["images"] = [x for x in slots if x in SLOTS]
    p["uploads"] = [x for x in slots if x in UPLOAD_SLOTS]
    return p


def generate(con, lead, fetch_html=None, fetch_image=None, overrides=None):
    """Build (or rebuild) the preview for a lead from the website, your uploads and your details.
    `overrides` replaces the saved details form; leave it None to keep what is saved."""
    existing = load(con, lead["id"]) or {}
    if overrides is not None:
        existing = dict(existing, overrides=overrides)
    profile, images = build_profile(lead, existing, fetch_html, fetch_image, get_uploads(con, lead["id"]))
    save(con, lead["id"], profile, images)
    return load(con, lead["id"])


# ---------------- hosted images (signed, unlisted, never logged) ----------------
def image_token(lead_id, slot, version):
    msg = f"{lead_id}.{slot}.{version}"
    return f"{msg}.{hmac.new(emailing._secret(), msg.encode(), hashlib.sha256).hexdigest()[:24]}"


def image_from_token(con, token):
    try:
        lead_id, slot, version, sig = token.split(".")
        good = hmac.new(emailing._secret(), f"{lead_id}.{slot}.{version}".encode(), hashlib.sha256).hexdigest()[:24]
        if slot not in SLOTS or not hmac.compare_digest(sig, good):
            return None
    except Exception:
        return None
    row = con.execute("SELECT content_type, data FROM preview_images WHERE lead_id=? AND slot=?", (int(lead_id), slot)).fetchone()
    return (row["content_type"], row["data"]) if row else None


def image_url(base_url, lead_id, slot, version):
    return f"{base_url}/i/{image_token(lead_id, slot, version)}"


# ---------------- rendering ----------------
def generated_logo_svg(name, brand):
    """A simple placeholder mark for the real store build: initials on the brand colour plus the name."""
    pal = palette(brand)
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "S"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="420" height="96" viewBox="0 0 420 96" role="img" aria-label="{E(name)}">'
            f'<rect x="4" y="8" width="80" height="80" rx="18" fill="{pal["brand"]}"/>'
            f'<text x="44" y="60" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="38" font-weight="700" fill="{pal["on_brand"]}">{E(initials)}</text>'
            f'<text x="100" y="60" font-family="Helvetica,Arial,sans-serif" font-size="30" font-weight="700" fill="{pal["ink"]}">{E(name[:22])}</text></svg>')


def theme_settings(profile):
    """Starting colours for a Dawn-based Shopify theme (Theme editor, Colors) taken from the preview."""
    pal = palette(profile["brand"], profile.get("accent"))
    scheme = lambda bg, text, btn, label: {"settings": {"background": bg, "background_gradient": "", "text": text, "button": btn,
                                                         "button_label": label, "secondary_button_label": btn, "shadow": text}}
    return {"note": "Starting point for a Dawn-based theme: paste into config/settings_data.json under current.color_schemes, "
                    "or enter the colours in Theme editor > Theme settings > Colors. Add the logo under Header.",
            "brand_color": pal["brand"], "accent_color": pal["accent"], "style": profile.get("style", "modern"),
            "serif_headings": profile.get("style") == "elegant",
            "color_schemes": {"scheme-1": scheme("#FFFFFF", pal["ink"], pal["brand"], pal["on_brand"]),
                              "scheme-2": scheme(pal["tint"], pal["ink"], pal["brand"], pal["on_brand"]),
                              "scheme-3": scheme(pal["brand"], pal["on_brand"], pal["on_brand"], pal["brand"])},
            "tagline": profile.get("tagline", ""), "logo": profile["logo"],
            "products": [{"name": p["name"], "price": p["price"], "sample": p["sample"]} for p in profile["products"]]}


def render_mockup(profile, base_url, lead_id, version):
    """A browser-framed store homepage in email-safe HTML. All text is escaped; images come from signed URLs."""
    pal, name = palette(profile["brand"], profile.get("accent")), profile["name"]
    style = STYLES.get(profile.get("style"), STYLES["modern"])
    head_font, body_font, radius = style["head"], "Helvetica,Arial,sans-serif", style["radius"]
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "S"
    if profile["logo"]["kind"] == "image" and "logo" in profile.get("images", ["logo"]):
        logo = (f'<img src="{E(image_url(base_url, lead_id, "logo", version))}" alt="{E(name)}" height="34" '
                f'style="display:block;border:0;height:34px;width:auto;max-width:150px">')
    else:
        logo = (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
                f'<td width="34" height="34" align="center" bgcolor="{pal["brand"]}" style="width:34px;height:34px;border-radius:9px;background:{pal["brand"]};'
                f'font-family:{body_font};font-size:15px;font-weight:700;color:{pal["on_brand"]}">{E(initials)}</td>'
                f'<td style="padding-left:9px;font-family:{head_font};font-size:17px;font-weight:700;color:{pal["ink"]}">{E(name[:22])}</td></tr></table>')
    cards = []
    for n, p in enumerate(profile["products"][:MAX_PRODUCTS], 1):
        slot = f"p{p.get('slot', n)}"
        if p.get("has_image") and slot in profile.get("images", [slot]):
            pic = (f'<img src="{E(image_url(base_url, lead_id, slot, version))}" alt="{E(p["name"])}" width="150" '
                   f'style="display:block;border:0;width:100%;max-width:150px;height:auto;border-radius:8px">')
        else:
            pic = (f'<div style="height:110px;border-radius:8px;background:{pal["tint"]};text-align:center;line-height:110px;'
                   f'font-family:{head_font};font-size:38px;font-weight:700;color:{pal["brand"]}">{E((p["name"] or "?")[0].upper())}</div>')
        price = (f'<div style="font-family:{body_font};font-size:13px;color:{pal["muted"]};padding-top:2px">{E(p["price"])}{" (sample)" if p["sample"] and p["price"] else ""}</div>'
                 if p["price"] or p["sample"] else "")
        cards.append(
            f'<td class="stack" width="33%" valign="top" style="padding:0 6px 10px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td>{pic}</td></tr>'
            f'<tr><td style="padding-top:8px;font-family:{body_font};font-size:14px;font-weight:600;color:{pal["ink"]}">{E(p["name"])}</td></tr>'
            f'<tr><td>{price}</td></tr><tr><td style="padding-top:8px"><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td bgcolor="{pal["accent"]}" style="border-radius:{radius}px;background:{pal["accent"]};padding:7px 14px;font-family:{body_font};font-size:12px;font-weight:700;color:{pal["on_accent"]}">Add to cart</td>'
            f'</tr></table></td></tr></table></td>')
    shop_url = f'{profile["slug"]}.mindlabfuture-ai.com'
    tagline = profile.get("tagline") or "Order online and pay by GCash, Maya or bank transfer."
    dots = "".join(f'<span style="display:inline-block;width:9px;height:9px;border-radius:50%;background:{c};margin-right:5px"></span>' for c in ("#FF6159", "#FFBD2E", "#28C940"))
    return (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 20px;border:1px solid {pal["line"]};border-radius:12px;background:#FFFFFF">'
        f'<tr><td style="background:#EEF1F6;padding:9px 12px;border-bottom:1px solid {pal["line"]};border-radius:12px 12px 0 0;font-family:{body_font};font-size:12px;color:{pal["muted"]}">{dots}&nbsp; {E(shop_url)}</td></tr>'
        f'<tr><td style="padding:14px 18px;border-bottom:1px solid {pal["line"]}"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td valign="middle">{logo}</td><td align="right" valign="middle" class="hide-sm" style="font-family:{body_font};font-size:13px;color:{pal["muted"]}">Shop &nbsp;&middot;&nbsp; About &nbsp;&middot;&nbsp; Contact</td></tr></table></td></tr>'
        f'<tr><td bgcolor="{pal["brand"]}" align="center" style="background:{pal["brand"]};padding:30px 18px">'
        f'<div style="font-family:{head_font};font-size:26px;font-weight:700;line-height:1.2;color:{pal["on_brand"]}">Welcome to {E(name)}</div>'
        f'<div style="font-family:{body_font};font-size:14px;line-height:1.5;color:{pal["on_brand"]};padding:8px 0 14px;opacity:.9">{E(tagline)}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr><td bgcolor="{pal["on_brand"]}" style="border-radius:{radius}px;background:{pal["on_brand"]};padding:10px 22px;font-family:{body_font};font-size:13px;font-weight:700;color:{pal["brand"]}">Shop now</td></tr></table></td></tr>'
        f'<tr><td style="padding:18px 12px 6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>{"".join(cards)}</tr></table></td></tr>'
        f'<tr><td align="center" style="padding:8px 12px 14px;border-top:1px solid {pal["line"]};font-family:{body_font};font-size:11px;color:{pal["muted"]}">Store preview</td></tr></table>')
