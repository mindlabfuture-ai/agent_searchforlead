"""A temporary demo storefront for a lead, published to Netlify at <business-name>.mindlabfuture-ai.com.

Guardrails, because it shows a real business's name on your domain without their say-so:
a banner on every page says it is a design preview and not a live store, nothing can be bought, sample products
are labelled, search engines are told to ignore it, it is deleted after DEMO_DAYS (30) days, and it is deleted
at once when the lead unsubscribes, bounces, is marked do-not-contact or lost. Only you publish one, from the
Previews page; the showcase email links to it only while it is live."""
import html
import io
import re
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone

from . import config, db, emailing, netlify, previews

E = lambda v: html.escape(str(v if v is not None else ""), quote=True)
EXT = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
FONTS = {  # head font, body font, Google Fonts family query
    "modern": ("Outfit", "Geist", "Outfit:wght@500;600;700&family=Geist:wght@400;500;600"),
    "elegant": ("Cormorant Garamond", "Geist", "Cormorant+Garamond:wght@500;600;700&family=Geist:wght@400;500;600"),
    "playful": ("Bricolage Grotesque", "Geist", "Bricolage+Grotesque:wght@500;600;700&family=Geist:wght@400;500;600"),
}
RADIUS = {"modern": 20, "elegant": 4, "playful": 28}
DEMO_DAYS = 30
BANNER = "Design preview by MindLab Future AI. This is not a live store and nothing here can be bought."
SENDER_EMAIL = "support@mindlabfuture-ai.com"


def demo_domain():
    return config.env("DEMO_DOMAIN", "mindlabfuture-ai.com")


def _rgba(hexv, alpha):
    h = hexv.lstrip("#")
    return "rgba(%d,%d,%d,%s)" % (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


# ---------------- the site ----------------
def build_files(profile, images):
    """{path: bytes} for the whole site. `images` maps slot (logo, p1..p3) to (content_type, data)."""
    pal = previews.palette(profile["brand"], profile.get("accent"))
    style = profile.get("style") if profile.get("style") in FONTS else "modern"
    head, body, family = FONTS[style]
    name = profile["name"]
    files, img_path = {}, {}
    for slot, (ctype, data) in images.items():
        if ctype in EXT and slot in previews.SLOTS:
            img_path[slot] = f"img/{slot}.{EXT[ctype]}"
            files[img_path[slot]] = data
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z0-9]+", name)[:2]).upper() or "S"
    if "logo" in img_path:
        logo = f'<img class="logo-img" src="{img_path["logo"]}" alt="{E(name)}" height="36">'
    else:
        logo = f'<span class="logo-mark" aria-hidden="true">{E(initials)}</span><span class="logo-name">{E(name[:28])}</span>'
    tagline = profile.get("tagline") or "Order online. Pay by GCash, Maya or bank transfer."
    products = profile["products"][:previews.MAX_PRODUCTS]
    tiles = []
    for n, p in enumerate(products):
        slot = f"p{p.get('slot', n + 1)}"
        pic = (f'<img src="{img_path[slot]}" alt="{E(p["name"])}" loading="lazy" width="800" height="800">' if slot in img_path
               else f'<span class="ph" aria-hidden="true">{E((p["name"] or "?")[0].upper())}</span>')
        price = f'<p class="price">{E(p["price"])}{" <em>(sample)</em>" if p["sample"] else ""}</p>' if p["price"] or p["sample"] else ""
        tiles.append(f'<article class="tile t{n + 1} reveal"><div class="shot">{pic}</div><div class="meta"><h3>{E(p["name"])}</h3>{price}'
                     f'<button type="button" class="buy">Add to cart</button></div></article>')
    hero_pic = (f'<img src="{img_path["p1"]}" alt="" width="900" height="1100">' if "p1" in img_path
                else f'<span class="ph big" aria-hidden="true">{E(initials)}</span>')
    second = (f'<div class="float reveal d3"><img src="{img_path["p2"]}" alt="" width="300" height="300"></div>' if "p2" in img_path else "")
    steps = [("Order", "Choose your items and check out from your phone."),
             ("Pay", "Send the payment by GCash, Maya or bank transfer."),
             ("Upload the receipt", "Add a screenshot of your receipt to confirm the order.")]
    steps_html = "".join(f'<li class="reveal d{i + 1}"><span class="num">{i + 1:02d}</span><div><h3>{E(t)}</h3><p>{E(d)}</p></div></li>' for i, (t, d) in enumerate(steps))
    niche = profile.get("niche") or ""
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">
<title>{E(name)} - design preview</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={family}&display=swap">
<link rel="stylesheet" href="style.css"></head>
<body>
<div class="banner" role="note">{E(BANNER)}</div>
<header class="top"><a class="brand" href="#top">{logo}</a>
<nav aria-label="Main"><a href="#shop">Shop</a><a href="#how">How it works</a><a href="#contact">Contact</a></nav></header>
<main id="top">
<section class="hero"><div class="copy">
<p class="eyebrow reveal">{E(niche)}</p>
<h1 class="reveal d1">{E(name)}</h1>
<p class="lead reveal d2">{E(tagline)}</p>
<p class="cta reveal d3"><a class="btn primary" href="#shop">Browse the shop</a><a class="btn ghost" href="#how">How ordering works</a></p></div>
<div class="visual"><div class="frame reveal d2">{hero_pic}</div>{second}</div></section>
<section id="shop" class="shop"><div class="head"><h2>The shop</h2><p>{"Sample products shown for this preview." if any(p["sample"] for p in products) else "A first selection."}</p></div>
<div class="grid n{len(products)}">{"".join(tiles)}</div></section>
<section id="how" class="how"><div class="head"><h2>How ordering works</h2><p>Simple for your customers, simple for you.</p></div><ol>{steps_html}</ol></section>
</main>
<footer id="contact"><p>Prepared for {E(name)} by <a href="https://mindlabfuture-ai.com" rel="noopener noreferrer">MindLab Future AI</a>, a Shopify Partner in Taguig, Philippines.</p>
<p>Like it? Email <a href="mailto:{SENDER_EMAIL}">{SENDER_EMAIL}</a>. Not interested? Reply STOP to our email and this page is removed.</p>
<p class="fine">{E(BANNER)}</p></footer>
<div class="toast" role="status" aria-live="polite" hidden>This is a design preview. Checkout is not enabled.</div>
<script src="app.js" defer></script></body></html>"""
    files["index.html"] = page.encode()
    brand, accent = pal["brand"], pal["accent"]
    ink = brand  # brand colour used as text: darken until it reads on the page background
    for k in range(1, 9):
        if previews.contrast(ink, "#FBFBFA") >= 4.5:
            break
        ink = previews.mix(brand, "#000000", k * 0.12)
    soft = previews.mix(brand, "#FFFFFF", 0.62)
    r = RADIUS[style]
    files["style.css"] = f"""*{{box-sizing:border-box;margin:0}}
:root{{--brand:{brand};--brand-ink:{ink};--soft:{soft};--on-brand:{pal["on_brand"]};--accent:{accent};--on-accent:{pal["on_accent"]};--tint:{pal["tint"]};--ink:{pal["ink"]};--muted:{pal["muted"]};--line:{pal["line"]};
--shadow:{_rgba(brand, .16)};--r:{r}px;--head:'{head}',Georgia,serif;--body:'{body}',system-ui,sans-serif}}
html{{scroll-behavior:smooth}}body{{font-family:var(--body);color:var(--ink);background:#FBFBFA;line-height:1.6;-webkit-font-smoothing:antialiased}}
img{{display:block;max-width:100%;height:auto}}a{{color:inherit}}
.banner{{background:var(--ink);color:#F4F4F2;font-size:13px;text-align:center;padding:9px 16px;letter-spacing:.01em}}
.top{{display:flex;align-items:center;justify-content:space-between;gap:16px;max-width:1280px;margin:0 auto;padding:18px 24px}}
.brand{{display:flex;align-items:center;gap:10px;text-decoration:none;font-family:var(--head);font-weight:600;font-size:20px}}
.logo-mark{{display:grid;place-items:center;width:38px;height:38px;border-radius:calc(var(--r)/2);background:var(--brand);color:var(--on-brand);font-size:15px;font-weight:700}}
.logo-img{{height:36px;width:auto;max-width:180px}}
.top nav{{display:flex;gap:22px;font-size:15px}}.top nav a{{text-decoration:none;color:var(--muted);transition:color .2s}}.top nav a:hover{{color:var(--ink)}}
.hero{{display:grid;grid-template-columns:1.1fr .9fr;gap:56px;align-items:center;max-width:1280px;margin:0 auto;padding:40px 24px 88px;min-height:min(78dvh,760px)}}
.eyebrow{{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--brand-ink);font-weight:600;margin-bottom:14px}}
h1{{font-family:var(--head);font-weight:600;font-size:clamp(2.4rem,5vw,4rem);line-height:1.04;letter-spacing:-.025em;max-width:14ch}}
.lead{{margin-top:18px;font-size:1.125rem;color:var(--muted);max-width:46ch}}
.cta{{margin-top:30px;display:flex;flex-wrap:wrap;gap:12px}}
.btn{{display:inline-block;padding:13px 24px;border-radius:999px;font-weight:600;font-size:15px;text-decoration:none;transition:transform .2s cubic-bezier(.16,1,.3,1),box-shadow .2s}}
.btn:active{{transform:translateY(1px) scale(.98)}}
.primary{{background:var(--brand);color:var(--on-brand);box-shadow:0 10px 24px -10px var(--shadow)}}.primary:hover{{transform:translateY(-2px)}}
.ghost{{border:1px solid var(--line);background:transparent}}.ghost:hover{{border-color:var(--ink)}}
.visual{{position:relative}}
.frame{{aspect-ratio:4/5;border-radius:var(--r);overflow:hidden;background:var(--soft);box-shadow:0 30px 60px -28px var(--shadow)}}
.frame img{{width:100%;height:100%;object-fit:cover}}
.float{{position:absolute;left:-36px;bottom:-30px;width:34%;aspect-ratio:1;border-radius:var(--r);overflow:hidden;border:6px solid #FBFBFA;box-shadow:0 20px 40px -20px var(--shadow)}}
.float img{{width:100%;height:100%;object-fit:cover}}
.ph{{display:grid;place-items:center;width:100%;height:100%;min-height:160px;font-family:var(--head);font-size:56px;font-weight:700;color:var(--brand-ink);background:var(--soft)}}.ph.big{{font-size:120px}}
.shop,.how{{max-width:1280px;margin:0 auto;padding:24px 24px 88px}}
.head{{display:flex;align-items:baseline;justify-content:space-between;gap:24px;border-top:1px solid var(--line);padding-top:24px;margin-bottom:36px}}
h2{{font-family:var(--head);font-weight:600;font-size:clamp(1.6rem,3vw,2.2rem);letter-spacing:-.02em}}.head p{{color:var(--muted);font-size:15px}}
.grid{{display:grid;gap:28px;grid-template-columns:1fr}}
.grid.n3{{grid-template-columns:1.4fr 1fr;grid-template-rows:auto auto}}.grid.n3 .t1{{grid-row:span 2}}
.grid.n2{{grid-template-columns:1.4fr 1fr}}.grid.n1{{grid-template-columns:minmax(0,520px)}}
.tile .shot{{aspect-ratio:1/1;border-radius:var(--r);overflow:hidden;background:var(--soft);transition:box-shadow .3s}}
.tile.t1 .shot{{aspect-ratio:4/5}}.tile:hover .shot{{box-shadow:0 24px 40px -24px var(--shadow)}}
.tile .shot img{{width:100%;height:100%;object-fit:cover;transition:transform .5s cubic-bezier(.16,1,.3,1)}}.tile:hover .shot img{{transform:scale(1.03)}}
.meta{{display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px;padding-top:14px}}.meta h3{{font-size:1.05rem;font-weight:600;flex:1 1 60%}}
.price{{color:var(--muted);font-size:15px}}.price em{{font-style:normal;font-size:12px}}
.buy{{font:inherit;font-weight:600;font-size:14px;padding:9px 18px;border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:var(--on-accent);cursor:pointer;transition:transform .2s}}
.buy:active{{transform:scale(.97)}}
.how ol{{list-style:none;padding:0;display:grid;gap:0}}.how li{{display:grid;grid-template-columns:96px 1fr;gap:20px;padding:28px 0;border-bottom:1px solid var(--line);max-width:760px}}
.how li:nth-child(2){{margin-left:clamp(0px,8vw,120px)}}.how li:nth-child(3){{margin-left:clamp(0px,16vw,240px)}}
.num{{font-family:var(--head);font-size:2.4rem;color:var(--brand-ink);line-height:1}}.how h3{{font-size:1.1rem;font-weight:600}}.how p{{color:var(--muted);margin-top:4px}}
footer{{background:var(--ink);color:#D9D9D6;padding:48px 24px 56px;display:grid;gap:10px;justify-items:start;padding-left:max(24px,calc((100vw - 1280px)/2 + 24px))}}
footer a{{color:#fff}}.fine{{font-size:12px;opacity:.7}}
.toast{{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);background:var(--ink);color:#fff;padding:12px 20px;border-radius:999px;font-size:14px;box-shadow:0 14px 30px -12px rgba(0,0,0,.4)}}
.reveal{{opacity:0;transform:translateY(14px);animation:rise .7s cubic-bezier(.16,1,.3,1) forwards}}
.d1{{animation-delay:.08s}}.d2{{animation-delay:.16s}}.d3{{animation-delay:.24s}}.t2{{animation-delay:.1s}}.t3{{animation-delay:.2s}}
@keyframes rise{{to{{opacity:1;transform:none}}}}
@media (max-width:820px){{.hero,.grid.n3,.grid.n2{{grid-template-columns:1fr}}.hero{{gap:36px;padding-bottom:56px}}.grid.n3 .t1{{grid-row:auto}}.float{{left:12px;bottom:-24px}}
.top nav{{gap:16px;font-size:14px}}.how li{{grid-template-columns:64px 1fr;margin-left:0!important}}.head{{flex-direction:column;gap:6px}}}}
@media (prefers-reduced-motion:reduce){{.reveal{{animation:none;opacity:1;transform:none}}html{{scroll-behavior:auto}}}}
""".encode()
    files["app.js"] = b"""(function(){var t=document.querySelector('.toast'),h;
document.querySelectorAll('.buy').forEach(function(b){b.addEventListener('click',function(){t.hidden=false;clearTimeout(h);h=setTimeout(function(){t.hidden=true},2600)})});
document.querySelectorAll('.shot img').forEach(function(i){i.addEventListener('error',function(){i.remove()})})})();
"""
    files["robots.txt"] = b"User-agent: *\nDisallow: /\n"
    files["_headers"] = (b"/*\n  X-Robots-Tag: noindex, nofollow, noarchive\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n"
                         b"  Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; "
                         b"font-src https://fonts.gstatic.com; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'\n")
    return files


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(files):
            z.writestr(zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0)), files[path])
    return buf.getvalue()


def site_zip(con, lead_id):
    """The demo site as a zip, or None if there is no preview yet."""
    pv = previews.load(con, lead_id)
    if not pv:
        return None
    rows = con.execute("SELECT slot, content_type, data FROM preview_images WHERE lead_id=? AND slot IN ('logo','p1','p2','p3')", (lead_id,)).fetchall()
    return make_zip(build_files(pv, {r["slot"]: (r["content_type"], r["data"]) for r in rows}))


# ---------------- publishing ----------------
def get(con, lead_id):
    return con.execute("SELECT * FROM demo_sites WHERE lead_id=?", (lead_id,)).fetchone()


def live_url(con, lead_id):
    d = get(con, lead_id)
    return d["url"] if d and d["status"] == "live" else None


def pick_slug(con, lead_id, name):
    base = previews.domain_slug(name)
    taken = {r[0] for r in con.execute("SELECT slug FROM demo_sites WHERE lead_id!=? AND status!='deleted'", (lead_id,))}
    slug, n = base, 2
    while slug in taken:
        slug = f"{base[:36]}-{n}"
        n += 1
    return slug


def client_from_env():
    return netlify.Client(config.env("NETLIFY_AUTH_TOKEN"), config.env("NETLIFY_ACCOUNT_SLUG"))


def publish(con, lead_id, client=None, now=None):
    """Create (or refresh) the live demo. Returns (ok, message)."""
    now = now or datetime.now(timezone.utc)
    lead = con.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    pv = previews.load(con, lead_id)
    if not lead or not pv:
        return False, "Build a preview first."
    if lead["status"] in ("do_not_contact", "lost", "merged") or con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (lead["url"],)).fetchone():
        return False, "This lead opted out or is closed, so no demo site is published."
    if lead["email"] and emailing.is_suppressed(con, lead["email"]):
        return False, "This lead opted out or bounced, so no demo site is published."
    try:
        client = client or client_from_env()
    except netlify.NetlifyError as e:
        return False, str(e)
    data = site_zip(con, lead_id)
    old = get(con, lead_id)
    expires = (now + timedelta(days=int(config.env_int("DEMO_DAYS", DEMO_DAYS)))).isoformat(timespec="seconds")
    try:
        if old and old["status"] == "live":  # refresh the same site with the latest preview
            client.deploy_zip(old["site_id"], data)
            con.execute("UPDATE demo_sites SET deployed_at=?, expires_at=? WHERE lead_id=?", (now.isoformat(timespec="seconds"), expires, lead_id))
            con.commit()
            return True, f"Updated {old['url']}."
        slug = pick_slug(con, lead_id, pv["name"])
        domain = f"{slug}.{demo_domain()}"
        site = client.create_site(f"mlf-demo-{slug}")
        site_id, default = site["id"], site.get("default_domain") or site.get("url", "").replace("https://", "").replace("http://", "")
        client.deploy_zip(site_id, data)
        note, url, zone_id, record_id = "", f"https://{default}" if default else "", None, None
        try:
            client.set_custom_domain(site_id, domain)
            zone = client.dns_zone(demo_domain())
            if zone:
                zone_id = zone["id"]
                recs = client.dns_records(zone_id)
                have = next((r for r in recs if r.get("hostname") == domain and r.get("type") in ("CNAME", "NETLIFY")), None)
                record_id = have["id"] if have else client.create_cname(zone_id, domain, default)["id"]
            try:
                client.provision_ssl(site_id)
            except netlify.NetlifyError as e:
                note = f"HTTPS certificate not ready yet ({str(e)[:80]}); the link may need a few minutes."
            url = f"https://{domain}"
        except netlify.NetlifyError as e:
            note = f"Published, but the custom address failed ({str(e)[:120]}). Using Netlify's own address."
        con.execute("INSERT OR REPLACE INTO demo_sites (lead_id,slug,site_id,dns_zone_id,dns_record_id,url,status,note,deployed_at,expires_at) VALUES (?,?,?,?,?,?,'live',?,?,?)",
                    (lead_id, slug, site_id, zone_id, record_id, url, note, now.isoformat(timespec="seconds"), expires))
        con.commit()
        return True, f"Published {url}." + (f" {note}" if note else "")
    except (netlify.NetlifyError, KeyError) as e:
        return False, f"Could not publish: {str(e)[:200]}"


def unpublish(con, lead_id, client=None, reason="removed by you"):
    """Delete the Netlify site and its DNS record. Returns (ok, message). A failed delete leaves the row 'live' so it is retried."""
    d = get(con, lead_id)
    if not d or d["status"] != "live":
        return True, "No live demo site."
    try:
        client = client or client_from_env()
        if d["dns_zone_id"] and d["dns_record_id"]:
            try:
                client.delete_dns_record(d["dns_zone_id"], d["dns_record_id"])
            except netlify.NetlifyError:
                pass  # the record disappears with the site in the usual case; do not block the takedown
        client.delete_site(d["site_id"])
    except netlify.NetlifyError as e:
        if "404" not in str(e):  # already gone counts as deleted
            return False, f"Could not delete the site yet: {str(e)[:160]}"
    con.execute("UPDATE demo_sites SET status='deleted', note=? WHERE lead_id=?", (reason, lead_id))
    con.commit()
    return True, f"Removed {d['url']} ({reason})."


def extend(con, lead_id, now=None):
    d = get(con, lead_id)
    if not d or d["status"] != "live":
        return "No live demo site."
    now = now or datetime.now(timezone.utc)
    con.execute("UPDATE demo_sites SET expires_at=? WHERE lead_id=?", ((now + timedelta(days=DEMO_DAYS)).isoformat(timespec="seconds"), lead_id))
    con.commit()
    return f"Extended by {DEMO_DAYS} days."


def _utc(ts):
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def cleanup(con, client=None, now=None, log=print):
    """Delete demo sites that have expired or whose lead opted out or was closed. Returns how many were removed."""
    now = now or datetime.now(timezone.utc)
    removed = 0
    for d in con.execute("SELECT * FROM demo_sites WHERE status='live'").fetchall():
        lead = con.execute("SELECT * FROM leads WHERE id=?", (d["lead_id"],)).fetchone()
        reason = None
        if _utc(d["expires_at"]) <= now:
            reason = "expired"
        elif not lead or lead["status"] in ("do_not_contact", "lost", "merged") or con.execute("SELECT 1 FROM do_not_contact WHERE url=?", (lead["url"],)).fetchone():
            reason = "lead opted out or closed"
        elif lead["email"] and emailing.is_suppressed(con, lead["email"]):
            reason = "lead unsubscribed or bounced"
        elif (previews.load(con, d["lead_id"]) or {}).get("status") == "skipped":
            reason = "showcase skipped"
        if reason:
            ok, msg = unpublish(con, d["lead_id"], client, reason)
            log(msg)
            removed += ok
    return removed
