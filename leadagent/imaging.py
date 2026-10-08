"""Uploaded images: decode them safely, strip metadata, shrink them, and read their dominant colours.

Uploads come from you, but the files themselves come from the internet, so every one is fully decoded and
re-encoded here. Nothing the browser sent is stored or served as-is."""
from io import BytesIO

from PIL import Image, ImageOps

from . import shopify_check

Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse decompression bombs instead of warning
MAX_UPLOAD_BYTES = 6_000_000

# Colours of the platforms' own interface. A screenshot of a Facebook page is full of them, and they say
# nothing about the business, so they are ignored when a colour comes from a screenshot.
UI_COLORS = ("#1877F2", "#0866FF", "#4267B2", "#E4E6EB", "#F0F2F5", "#65676B", "#E1306C", "#833AB4", "#F56040", "#FCAF45", "#0095F6")


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(c):
    return "#{:02X}{:02X}{:02X}".format(*c)


def _dist(a, b):
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def normalize_upload(data, kind="photo", max_side=1600):
    """Return (content_type, bytes) for a clean re-encoded copy. Raises ValueError for anything that is not a
    real, reasonably sized PNG, JPEG, GIF or WebP image. `kind='logo'` keeps transparency."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file is too large (the limit is {MAX_UPLOAD_BYTES // 1_000_000} MB)")
    if not shopify_check.sniff_image(data):
        raise ValueError("that is not a PNG, JPEG, GIF or WebP image")
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        raise ValueError("that image could not be read (it may be damaged)")
    img = ImageOps.exif_transpose(img)  # honour the phone's rotation, then drop all metadata by re-encoding
    if getattr(img, "is_animated", False):
        img.seek(0)
    img.thumbnail((max_side, max_side))
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    out = BytesIO()
    if kind == "logo" and has_alpha:
        img.convert("RGBA").save(out, "PNG", optimize=True)
        return "image/png", out.getvalue()
    if has_alpha:  # a photo with transparency: flatten on white
        flat = Image.new("RGB", img.size, "white")
        flat.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[3])
        img = flat
    img.convert("RGB").save(out, "JPEG", quality=86, optimize=True)
    return "image/jpeg", out.getvalue()


def dominant_colors(data, n=8):
    """[(hex, share)] for the n most common colours, ignoring fully transparent pixels. Share sums to about 1."""
    img = Image.open(BytesIO(data)).convert("RGBA")
    img.thumbnail((120, 120))
    flat = Image.new("RGB", img.size, (255, 255, 255))
    flat.paste(img, mask=img.split()[3])
    q = flat.quantize(colors=n, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[:n * 3]
    total = sum(c for c, _ in q.getcolors()) or 1
    out = [(_hex(tuple(pal[i * 3:i * 3 + 3])), c / total) for c, i in sorted(q.getcolors(), reverse=True)]
    return out


def _vivid(rgb):
    import colorsys
    r, g, b = (v / 255 for v in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    return h * 360, l, s


def analyze_palette(sources):
    """Pick a brand colour and an optional accent from images.

    `sources` is a list of (image_bytes, kind) with kind 'product', 'logo' or 'screenshot'. Product photos carry
    the most weight (that is what the store will show), logos count a bit more per pixel, screenshots less and
    have the platform's own interface colours removed. Returns
    {"brand": hex|None, "accent": hex|None, "colors": [hex...], "basis": text}."""
    weight = {"product": 1.0, "logo": 1.6, "screenshot": 0.5}
    clusters = []  # [rgb, score]
    used = set()
    for data, kind in sources:
        try:
            colors = dominant_colors(data)
        except Exception:
            continue
        used.add(kind)
        for h, share in colors:
            rgb = _rgb(h)
            if kind == "screenshot" and any(_dist(rgb, _rgb(u)) < 45 for u in UI_COLORS):
                continue
            _, l, s = _vivid(rgb)
            if s < 0.2 or l > 0.92 or l < 0.1:  # white, black and grey backgrounds are not a brand colour
                continue
            score = share * weight[kind] * (0.4 + s)
            for c in clusters:
                if _dist(c[0], rgb) < 42:
                    c[1] += score
                    break
            else:
                clusters.append([rgb, score])
    clusters.sort(key=lambda c: -c[1])
    basis = " and ".join(x for x in ("product photos" if "product" in used else "", "logo" if "logo" in used else "",
                                    "screenshots" if "screenshot" in used else "") if x)
    if not clusters:
        return {"brand": None, "accent": None, "colors": [], "basis": basis}
    brand = clusters[0][0]
    bh = _vivid(brand)[0]
    accent = None
    for c, score in clusters[1:]:
        dh = abs(_vivid(c)[0] - bh)
        if min(dh, 360 - dh) >= 35 and score >= clusters[0][1] * 0.15:
            accent = c
            break
    return {"brand": _hex(brand), "accent": _hex(accent) if accent else None, "colors": [_hex(c[0]) for c in clusters[:5]], "basis": basis}
