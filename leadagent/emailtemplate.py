"""HTML email in the MindLab Future AI brand (colours, fonts and logo taken from mindlabfuture-ai.com):
dark navy card, brass accents, Space Grotesk headings. Table layout with inline styles and bgcolor
fallbacks so it holds up in Gmail, Outlook and Apple Mail. A plain-text version is always sent alongside."""
import html as H
from urllib.parse import quote

# Brand tokens from the website's stylesheet.
BG, CARD, CARD2, LINE = "#060A12", "#0E1727", "#121D31", "#1E2A3D"
TEXT, MUTED = "#EDF2FA", "#9AA8BF"
BRASS, BRASS_HI, BRASS_INK = "#E3B965", "#F3D28F", "#1A1303"
HEAD = "'Space Grotesk','Segoe UI',Helvetica,Arial,sans-serif"
BODY = "Inter,'Segoe UI',Helvetica,Arial,sans-serif"

LOGO_URL = "https://mindlabfuture-ai.com/img/logo-ml.png"  # 216x194 PNG with transparency
SITE_URL = "https://mindlabfuture-ai.com"

E = lambda v: H.escape(str(v), quote=True)

CHIPS = ("Product pages", "GCash, Maya &amp; bank checkout", "Shipping set up")


def _p(text, color=TEXT, size=16, extra=""):
    return (f"<p style=\"margin:0 0 16px;font-family:{BODY};font-size:{size}px;line-height:1.6;color:{color};{extra}\">"
            f"{E(text)}</p>")


def _offer_item(text):
    """Bold the lead-in before a short 'Label: ...' so the three terms are scannable."""
    head, sep, rest = text.partition(": ")
    body = f"<strong style=\"color:{BRASS_HI};font-weight:600\">{E(head)}:</strong> {E(rest)}" if sep and len(head) <= 40 else E(text)
    return (f"<tr><td width=\"22\" valign=\"top\" style=\"padding:0 0 10px;font-size:15px;line-height:1.55;color:{BRASS}\">&#10003;</td>"
            f"<td style=\"padding:0 0 10px;font-family:{BODY};font-size:15px;line-height:1.55;color:{TEXT}\">{body}</td></tr>")


def _offer_card(title, items, notes=()):
    if not items:
        return ""
    rows = "".join(_offer_item(i) for i in items)
    return (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" style=\"margin:8px 0 20px\"><tr>"
            f"<td bgcolor=\"{CARD2}\" style=\"background:{CARD2};border:1px solid {BRASS};border-radius:14px;padding:20px 22px 10px\">"
            f"<div style=\"margin:0 0 12px;font-family:{HEAD};font-size:17px;font-weight:600;letter-spacing:-0.01em;color:{BRASS_HI}\">{E(title.rstrip(':'))}</div>"
            f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\">{rows}</table></td></tr></table>"
            + "".join(_p(n, MUTED, 13, "margin:-8px 0 18px;") for n in notes))


def render_html(*, subject, preheader, greeting, found, pitch, ask, reply, signature, cta_label, cta_mailto,
                unsub_url, source, company, address, callout="", offer_title="", offer_items=(), offer_notes=(),
                logo_url=LOGO_URL, site_url=SITE_URL):
    chips = "".join(
        f"<td class=\"stack\" width=\"33%\" valign=\"top\" style=\"padding:0 4px 8px\">"
        f"<table role=\"presentation\" width=\"100%\" height=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\"><tr>"
        f"<td class=\"chip\" bgcolor=\"{CARD2}\" height=\"56\" valign=\"middle\" style=\"background:{CARD2};border:1px solid {LINE};border-radius:12px;padding:12px 14px;"
        f"font-family:{BODY};font-size:13px;line-height:1.4;color:{TEXT}\">"
        f"<span style=\"color:{BRASS};font-size:15px\">&#9670;</span>&nbsp; {c}</td></tr></table></td>" for c in CHIPS)
    callout_html = (f"<table role=\"presentation\" width=\"100%\" cellpadding=\"0\" cellspacing=\"0\" border=\"0\" "
                    f"style=\"margin:0 0 16px\"><tr><td style=\"border-left:3px solid {BRASS};padding:4px 0 4px 14px;"
                    f"font-family:{BODY};font-size:15px;line-height:1.6;color:{MUTED}\">{E(callout)}</td></tr></table>"
                    if callout else "")
    sign = "<br>".join(E(line) for line in signature)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light"><meta name="supported-color-schemes" content="dark light">
<title>{E(subject)}</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>@media only screen and (max-width:620px){{.px{{padding-left:22px!important;padding-right:22px!important}}.stack{{display:block!important;width:100%!important;padding:0 0 8px!important}}.h1{{font-size:24px!important}}.hide-sm{{display:none!important}}.chip{{height:auto!important}}}}</style>
</head>
<body style="margin:0;padding:0;background:{BG}" bgcolor="{BG}">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{BG};font-size:1px;line-height:1px">{E(preheader)}&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="{BG}" style="background:{BG}">
<tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:600px">

<tr><td class="px" style="padding:4px 4px 18px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="56" valign="middle"><a href="{site_url}"><img src="{logo_url}" width="48" height="43" alt="MindLab Future AI" style="display:block;border:0;width:48px;height:43px;color:{BRASS_HI};font-family:{HEAD};font-size:14px"></a></td>
<td valign="middle" style="font-family:{HEAD};font-size:18px;font-weight:600;letter-spacing:-0.01em;color:{TEXT}">MindLab Future AI</td>
<td class="hide-sm" align="right" valign="middle" style="font-family:{BODY};font-size:12px;color:{MUTED}">Shopify Partner &middot; Taguig, PH</td>
</tr></table></td></tr>

<tr><td height="3" bgcolor="{BRASS}" style="height:3px;line-height:3px;font-size:0;background:{BRASS};background-image:linear-gradient(90deg,{BRASS_HI},{BRASS});border-radius:3px 3px 0 0">&nbsp;</td></tr>

<tr><td class="px" bgcolor="{CARD}" style="background:{CARD};padding:34px 36px 12px;border-left:1px solid {LINE};border-right:1px solid {LINE}">
<h1 class="h1" style="margin:0 0 20px;font-family:{HEAD};font-size:28px;line-height:1.2;font-weight:600;letter-spacing:-0.02em;color:{TEXT}">{E(subject)}</h1>
{_p(greeting)}{_p(found)}{_p(pitch)}{callout_html}
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 12px"><tr>{chips}</tr></table>
{_offer_card(offer_title, offer_items, offer_notes)}{_p(ask)}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 22px"><tr>
<td align="center" bgcolor="{BRASS}" style="border-radius:999px;background:{BRASS};background-image:linear-gradient(180deg,{BRASS_HI},{BRASS})">
<a href="{E(cta_mailto)}" style="display:inline-block;padding:15px 28px;font-family:{BODY};font-size:15px;font-weight:600;color:{BRASS_INK};text-decoration:none;border-radius:999px">{E(cta_label)}</a></td></tr></table>
{_p(reply, MUTED, 14)}
</td></tr>

<tr><td class="px" bgcolor="{CARD}" style="background:{CARD};padding:6px 36px 30px;border:1px solid {LINE};border-top:0;border-radius:0 0 14px 14px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-top:1px solid {LINE}"><tr>
<td style="padding-top:20px;font-family:{BODY};font-size:15px;line-height:1.5;color:{TEXT}"><strong style="font-family:{HEAD};font-weight:600">{sign}</strong></td></tr></table>
</td></tr>

<tr><td class="px" style="padding:20px 8px 8px;font-family:{BODY};font-size:12px;line-height:1.6;color:{MUTED};text-align:center">
You're getting this one-time message because this business address is publicly listed at {E(source)}.<br>
{E(company)} &middot; {E(address)}<br>
<a href="{E(site_url)}" style="color:{BRASS_HI};text-decoration:underline">mindlabfuture-ai.com</a> &nbsp;&middot;&nbsp;
<a href="{E(unsub_url)}" style="color:{BRASS_HI};text-decoration:underline">Unsubscribe</a> or just reply STOP.
</td></tr>

</table></td></tr></table></body></html>"""


def cta_mailto(reply_to, name, taglish=False):
    subject = ("Libreng store preview para sa " if taglish else "Free store preview for ") + name
    return f"mailto:{reply_to}?subject={quote(subject)}"
