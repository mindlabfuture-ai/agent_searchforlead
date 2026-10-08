"""Dashboard pages for store previews: upload screenshots, a logo and product photos, add details, review the
exact showcase email, then approve it. Rendering and form handling only; routing lives in server.py."""
import email.policy
import html as H
from datetime import datetime, timezone
from email import message_from_bytes

from . import config, demosite, db, emailing, imaging, previews, showcase

E = lambda v: H.escape(str(v if v is not None else ""), quote=True)
MAX_FORM_BYTES = 20_000_000
FILE_FIELDS = {"up_logo": "logo", "up_p1": "photo", "up_p2": "photo", "up_p3": "photo", "shot1": "photo", "shot2": "photo", "shot3": "photo"}


def parse_multipart(content_type, body):
    """(fields, files) from a multipart/form-data body, using only the standard library. Empty file inputs are dropped."""
    msg = message_from_bytes(b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n" + body,
                             policy=email.policy.HTTP)
    fields, files = {}, {}
    if not msg.is_multipart():
        return fields, files
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        if part.get_filename() is not None:
            if payload:
                files[name] = payload
        else:
            fields[name] = payload.decode("utf-8", "replace")
    return fields, files


def _lead(con, lead_id):
    return con.execute("SELECT * FROM leads WHERE id=? AND status!='merged'", (lead_id,)).fetchone()


def _timeline(con, lead, now=None):
    first = showcase.first_email_at(con, lead["id"])
    if not first:
        return "No first email sent yet."
    d = ((now or datetime.now(timezone.utc)) - first).days
    sent = showcase.showcase_sent(con, lead["id"])
    return (f"First email sent {first.date().isoformat()} ({d} day{'s' if d != 1 else ''} ago). "
            + ("The showcase email has been sent." if sent else "Due for the showcase email." if d >= showcase.DAYS_UNTIL_SHOWCASE and lead["status"] == "contacted"
               else f"Showcase is due on day {showcase.DAYS_UNTIL_SHOWCASE}." if lead["status"] == "contacted" else f"Lead status is '{lead['status']}', so no showcase will be sent."))


def render_list(con, tok, now=None, message=""):
    now = now or datetime.now(timezone.utc)
    rows = con.execute("SELECT * FROM leads WHERE status='contacted' ORDER BY id DESC LIMIT 200").fetchall()
    due, waiting, done = [], [], []
    for l in rows:
        d = showcase.days_since_first(con, l["id"], now)
        pv = previews.load(con, l["id"])
        label = f"<a href='/previews/{l['id']}'>{E(l['name'] or l['url'])}</a> <small>#{l['id']} &middot; {E(l['platform'])}"
        if d is not None:
            label += f" &middot; first email {d} day{'s' if d != 1 else ''} ago"
        label += f" &middot; preview: {E(pv['status'] if pv else 'not built')}</small>"
        (done if showcase.showcase_sent(con, l["id"]) else due if showcase.is_due(con, l, now) else waiting).append(f"<li>{label}</li>")
    sec = lambda title, items, empty: f"<section><h3>{title}</h3><ul>{''.join(items) or f'<li>{empty}</li>'}</ul></section>"
    msg = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Previews</title>{PAGE_STYLE()}"
            f"<p><a href='/'>&larr; Lead queue</a> &middot; <a href='/clients'>Clients</a></p><h1>Store previews</h1>{msg}"
            f"<p>Upload their page screenshots, logo and product photos, add details, and the agent builds a branded store preview. "
            f"Seven days after the first email you review it here and approve the showcase email.</p>"
            f"<section><form method=get action='/previews/open' style=display:inline>Open the editor for lead # "
            f"<input type=text name=id size=6> <button>Open</button></form></section>"
            + sec("Showcase due (7+ days since the first email)", due, "Nothing due yet.")
            + sec("Contacted, waiting for day 7", waiting, "None.") + sec("Showcase already sent", done, "None yet."))


def PAGE_STYLE():
    from . import server
    return server.PAGE_STYLE


def _swatch(hexv, label=""):
    return (f"<span style='display:inline-block;margin:2px 6px 2px 0'><span style='display:inline-block;width:22px;height:22px;border-radius:5px;"
            f"background:{E(hexv)};vertical-align:middle;border:1px solid #ccc'></span> <small>{E(label or hexv)}</small></span>")


def render_editor(con, lead_id, tok, message="", now=None):
    lead = _lead(con, lead_id)
    if not lead:
        return None
    pv = previews.load(con, lead_id)
    ov = (pv or {}).get("overrides", {})
    given = ov.get("products") or []
    prod = lambda i, k: E((given[i - 1].get(k, "") if i - 1 < len(given) and isinstance(given[i - 1], dict) else ""))
    msg = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    can_render = bool(pv) and len(config.env("UNSUB_SECRET")) >= 16 and config.base_url() and lead["email"]
    if can_render:
        mail = showcase.build_showcase(lead, pv, config.base_url(), demosite.live_url(con, lead_id) or "")
        preview = (f"<p><b>Subject:</b> {E(mail['subject'])}</p><iframe sandbox style='width:100%;height:1400px;border:1px solid #ccc;border-radius:8px' "
                   f"srcdoc=\"{E(mail['html'])}\"></iframe>")
    elif not pv:
        preview = "<p>No preview built yet. Add what you have below (everything is optional) and press Save and build.</p>"
    else:
        preview = "<p class=no>To see the exact email, set a business email for this lead and make sure BASE_URL and UNSUB_SECRET are configured.</p>"
    info = ""
    if pv:
        sw = "".join(_swatch(c) for c in pv.get("swatches", [])[:5])
        info = (f"<p><b>Brand colour:</b> {_swatch(pv['brand'], pv['brand'] + ' (' + pv['brand_note'] + ')')}"
                f"{_swatch(pv['accent'], 'accent ' + pv['accent']) if pv.get('accent') else ''}</p>"
                f"<p><b>Colours the agent found:</b> {sw or 'none'}</p>"
                f"<p><b>Logo:</b> {'image (' + E(pv['logo']['source']) + ')' if pv['logo']['kind'] == 'image' else 'generated placeholder'} &middot; "
                f"<b>Style:</b> {E(pv.get('style'))} &middot; <b>Products:</b> {', '.join(E(p['name']) + (' (sample)' if p['sample'] else '') for p in pv['products'])}</p>"
                f"<p><small>Notes: {E('; '.join(pv.get('notes', [])) or 'none')} &middot; status: <b>{E(pv['status'])}</b></small></p>")
    thumbs = "".join(f"<span style='display:inline-block;margin:4px;text-align:center'><img src='/pimg/{lead_id}/{s}' height=70 style='border:1px solid #ccc;border-radius:6px'><br>"
                     f"<small>{E(s)}</small></span>" for s in (pv or {}).get("uploads", []))
    btn = lambda action, label: (f"<form method=post action='/previews/{lead_id}/action' style=display:inline><input type=hidden name=csrf value={tok}>"
                                 f"<button name=action value={action}>{label}</button></form>")
    status = (pv or {}).get("status")
    actions = ((btn("unapprove", "Withdraw approval") if status == "approved" else btn("approve", "Approve the showcase email")) if pv else "") \
        + btn("skip", "Skip the showcase") + btn("replied", "They replied") + btn("dnc", "Do not contact")
    downloads = (f" <a href='/previews/{lead_id}/theme.json'>theme colours (JSON)</a> &middot; <a href='/previews/{lead_id}/logo.svg'>generated logo (SVG)</a>" if pv else "")
    demo = demosite.get(con, lead_id)
    token_set = bool(config.env("NETLIFY_AUTH_TOKEN"))
    if demo and demo["status"] == "live":
        demo_html = (f"<p>Live: <a href=\"{E(demo['url'])}\" rel='noopener noreferrer' target=_blank>{E(demo['url'])}</a> &middot; expires {E(demo['expires_at'][:10])}"
                     f"{' &middot; <span class=no>' + E(demo['note']) + '</span>' if demo['note'] else ''}</p>"
                     f"<p>{btn('demo_publish', 'Update with the latest preview')}{btn('demo_extend', 'Extend 30 days')}{btn('demo_unpublish', 'Take it down')}</p>"
                     f"<p><small>The showcase email links to this page while it is live.</small></p>")
    elif pv:
        demo_html = ((f"<p>{btn('demo_publish', 'Publish demo site')}</p>" if token_set else "<p class=no>Set NETLIFY_AUTH_TOKEN to publish. You can still download the site below.</p>")
                     + "<p><small>Publishes a temporary storefront at the business-name address. It is marked as a design preview, hidden from search, "
                       "deleted after 30 days, and deleted at once if the lead opts out.</small></p>")
    else:
        demo_html = "<p>Build a preview first.</p>"
    demo_dl = f" <a href='/previews/{lead_id}/demo.zip'>demo site (zip)</a>" if pv else ""
    file_in = lambda name, label: (f"<label>{label}</label><input type=file name={name} accept='image/png,image/jpeg,image/gif,image/webp'>"
                                   + (f" <label style=display:inline><input type=checkbox name=rm_{name} value=1> remove the uploaded one</label>" if pv and name in pv.get("uploads", []) else ""))
    style_opts = "".join(f"<option value={k}{' selected' if ov.get('style') == k else ''}>{E(v['label'])}</option>" for k, v in previews.STYLES.items())
    niche_opts = "<option value=''>Detect automatically</option>" + "".join(
        f"<option{' selected' if ov.get('niche') == n else ''}>{E(n)}</option>" for n in sorted(previews.SAMPLES))
    products_form = "".join(
        f"<fieldset style='border:1px solid #ddd;border-radius:8px;margin:8px 0'><legend>Product {i}</legend><label>Name</label><input type=text name=p{i}_name value='{prod(i, 'name')}'>"
        f"<label>Price (for example ₱499)</label><input type=text name=p{i}_price value='{prod(i, 'price')}'>"
        f"{file_in(f'up_p{i}', 'Photo')}<label>Or a photo address (right-click the image on their page, Copy image address)</label>"
        f"<input type=text name=p{i}_url value='{prod(i, 'image_url')}'></fieldset>" for i in (1, 2, 3))
    form = (f"<form method=post action='/previews/{lead_id}/save' enctype='multipart/form-data'><input type=hidden name=csrf value={tok}>"
            f"<fieldset style='border:1px solid #ddd;border-radius:8px;margin:8px 0'><legend>Their page</legend>"
            f"{file_in('shot1', 'Screenshot 1')}{file_in('shot2', 'Screenshot 2')}{file_in('shot3', 'Screenshot 3')}"
            f"<p><small>Tip: crop each screenshot to the product photos. The agent picks the palette from the products and ignores Facebook's and "
            f"Instagram's own blue, but anything else in the picture counts.</small></p></fieldset>"
            f"<fieldset style='border:1px solid #ddd;border-radius:8px;margin:8px 0'><legend>Logo</legend>{file_in('up_logo', 'Upload their logo')}"
            f"<label>Or a logo address</label><input type=text name=logo_url value='{E(ov.get('logo_url', ''))}'>"
            f"<p><small>No logo? Leave both empty and the agent generates a simple placeholder with their initials.</small></p></fieldset>{products_form}"
            f"<fieldset style='border:1px solid #ddd;border-radius:8px;margin:8px 0'><legend>Details</legend>"
            f"<label>Store name (blank = {E(lead['name'] or 'the lead name')})</label><input type=text name=store_name value='{E(ov.get('store_name', ''))}'>"
            f"<label>Tagline shown on the store</label><input type=text name=tagline value='{E(ov.get('tagline', ''))}' maxlength=120>"
            f"<label>Look and feel</label><select name=style>{style_opts}</select>"
            f"<label>What they sell</label><select name=niche>{niche_opts}</select>"
            f"<label>Brand colour (blank = let the agent decide), for example #8B4513</label><input type=text name=brand value='{E(ov.get('brand', ''))}'>"
            f"<label>Accent colour (optional)</label><input type=text name=accent value='{E(ov.get('accent', ''))}'>"
            f"<label>Their business email (only if publicly listed)</label><input type=text name=email value='{E(lead['email'])}'>"
            f"<label>Notes for yourself (never emailed)</label><input type=text name=notes value='{E(ov.get('notes', ''))}'></fieldset>"
            f"<p><button>Save and build the preview</button></p></form>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Preview: {E(lead['name'] or lead['url'])}</title>{PAGE_STYLE()}"
            f"<p><a href='/previews'>&larr; Previews</a> &middot; <a href='/'>Lead queue</a></p><h1>{E(lead['name'] or lead['url'])} <small>#{lead_id}</small></h1>{msg}"
            f"<p><a href=\"{E(lead['url'])}\" rel='noopener noreferrer' target=_blank>{E(lead['url'])}</a>"
            f"{' &middot; website: ' + E(lead['website']) if lead['website'] else ''} &middot; email: {E(lead['email'] or 'none')}</p><p>{E(_timeline(con, lead, now))}</p>"
            f"{info}{('<p>' + thumbs + '</p>') if thumbs else ''}<section><h3>Your actions</h3><p>{actions}{downloads}{demo_dl}</p></section>"
            f"<section><h3>Demo site</h3>{demo_html}</section>"
            f"<section><h3>The showcase email, exactly as it will be sent</h3>{preview}</section><section><h3>Add what you have</h3>{form}</section>")


def save_from_form(con, lead_id, fields, files, fetch_html=None, fetch_image=None):
    """Store uploads, update the details, rebuild the preview. Returns a message for the page."""
    lead = _lead(con, lead_id)
    if not lead:
        return "Unknown lead."
    problems = []
    for name, kind in FILE_FIELDS.items():
        if fields.get(f"rm_{name}"):
            previews.clear_upload(con, lead_id, name)
        if name in files:
            try:
                ctype, data = imaging.normalize_upload(files[name], "logo" if kind == "logo" else "photo")
                previews.store_upload(con, lead_id, name, ctype, data)
            except ValueError as e:
                problems.append(f"{name}: {e}")
    ov = {"store_name": fields.get("store_name", "")[:60], "tagline": fields.get("tagline", "")[:120], "style": fields.get("style", ""),
          "niche": fields.get("niche", ""), "notes": fields.get("notes", "")[:300], "products": []}
    for key in ("brand", "accent"):
        v = (fields.get(key) or "").strip()
        if v and not previews.norm_hex(v):
            problems.append(f"{key}: '{v[:12]}' is not a colour like #8B4513")
        ov[key] = previews.norm_hex(v) or ""
    for key in ("logo_url",):
        v = (fields.get(key) or "").strip()
        if v and not v.lower().startswith("https://"):
            problems.append("logo address must start with https://"); v = ""
        ov[key] = v
    for i in (1, 2, 3):
        url = (fields.get(f"p{i}_url") or "").strip()
        if url and not url.lower().startswith("https://"):
            problems.append(f"product {i} photo address must start with https://"); url = ""
        ov["products"].append({"name": fields.get(f"p{i}_name", "")[:60], "price": fields.get(f"p{i}_price", "")[:20], "image_url": url})
    new_email = emailing.extract_emails(fields.get("email", ""))
    if fields.get("email", "").strip():
        if new_email:
            con.execute("UPDATE leads SET email=?, email_source=COALESCE(NULLIF(email_source,''), 'added manually') WHERE id=?", (new_email[0], lead_id))
            con.commit()
        else:
            problems.append("email: not a usable business address")
    previews.generate(con, _lead(con, lead_id), fetch_html, fetch_image, overrides=ov)
    return "Saved and rebuilt the preview." + (" Problems: " + "; ".join(problems) if problems else "")


def apply_action(con, lead_id, action, netlify_client=None):
    """approve | unapprove | skip | replied | dnc | demo_publish | demo_unpublish | demo_extend. Returns a message."""
    lead = _lead(con, lead_id)
    if not lead:
        return "Unknown lead."
    pv = previews.load(con, lead_id)
    t = db.now()
    if action == "approve":
        if not pv:
            return "Build a preview first."
        if not lead["email"]:
            return "Add a business email first."
        con.execute("UPDATE previews SET status='approved', approved_at=?, updated_at=? WHERE lead_id=?", (t, t, lead_id))
        msg = "Approved. It will send on the next run inside business hours once it is due."
    elif action == "unapprove" and pv:
        con.execute("UPDATE previews SET status='draft', approved_at=NULL WHERE lead_id=?", (lead_id,))
        msg = "Approval withdrawn."
    elif action == "skip" and pv:
        con.execute("UPDATE previews SET status='skipped' WHERE lead_id=?", (lead_id,))
        msg = "No showcase email will be sent for this lead."
    elif action == "replied":
        con.execute("UPDATE leads SET status='replied', updated_at=? WHERE id=?", (t, lead_id))
        msg = "Marked as replied, so no showcase email will be sent."
    elif action == "dnc":
        db.add_do_not_contact(con, lead["url"], "marked in the previews page")
        if lead["email"]:
            emailing.suppress(con, lead["email"], "marked in the previews page")
        msg = "Marked do not contact."
        if demosite.get(con, lead_id):
            msg += " " + demosite.unpublish(con, lead_id, netlify_client, "lead opted out")[1]
    elif action == "demo_publish":
        msg = demosite.publish(con, lead_id, netlify_client)[1]
    elif action == "demo_unpublish":
        msg = demosite.unpublish(con, lead_id, netlify_client)[1]
    elif action == "demo_extend":
        msg = demosite.extend(con, lead_id)
    else:
        msg = "Nothing changed."
    con.commit()
    return msg
