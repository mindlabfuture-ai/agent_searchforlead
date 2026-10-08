"""Decide whether a seller already has a Shopify store, using only their own public website."""
import re
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; MindLabLeadBot/1.0; +https://mindlabfuture-ai.com)"
MARKERS = ["cdn.shopify.com", "myshopify.com", "shopify.theme", "shopify-checkout-api-token",
           "x-shopid", "shopify-features", "powered by shopify"]
MARKETPLACE_PLATFORMS = {"shopee", "lazada", "carousell", "tiktok"}
MARKETPLACES = ["shopee.", "lazada.", "tiktok.com/@", "carousell."]


def classify_html(html, headers=None):
    blob = (html or "").lower() + " " + " ".join(f"{k}: {v}" for k, v in (headers or {}).items()).lower()
    return "has_shopify" if any(m in blob for m in MARKERS) else "no_store"


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(300_000).decode("utf-8", "ignore"), dict(r.headers)


def check_website(website, fetch_fn=fetch, platform="facebook"):
    """Returns has_shopify | no_store | marketplace_only | unknown."""
    if not website:
        # Nothing linked. On a marketplace that means they only sell there; elsewhere, social-only.
        # Either way, verify by eye before outreach.
        return "marketplace_only" if platform in MARKETPLACE_PLATFORMS else "no_store"
    if any(m in website.lower() for m in MARKETPLACES):
        return "marketplace_only"
    if not re.match(r"https?://", website):
        website = "https://" + website
    try:
        html, headers = fetch_fn(website)
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown"
    return classify_html(html, headers)
