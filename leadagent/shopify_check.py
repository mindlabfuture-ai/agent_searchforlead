"""Decide whether a seller already has a Shopify store, using only their own public website."""
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

UA = "Mozilla/5.0 (compatible; MindLabLeadBot/1.0; +https://mindlabfuture-ai.com)"
MARKERS = ["cdn.shopify.com", "myshopify.com", "shopify.theme", "shopify-checkout-api-token",
           "x-shopid", "shopify-features", "powered by shopify"]
MARKETPLACE_PLATFORMS = {"shopee", "lazada", "carousell", "tiktok"}
MARKETPLACES = ["shopee.", "lazada.", "tiktok.com/@", "carousell."]


def classify_html(html, headers=None):
    blob = (html or "").lower() + " " + " ".join(f"{k}: {v}" for k, v in (headers or {}).items()).lower()
    return "has_shopify" if any(m in blob for m in MARKERS) else "no_store"


def assert_public(url):
    """Refuse anything but plain http(s) on 80/443 to a public address. Lead websites come from search
    results and pasted CSVs, so they must never make this server call internal or metadata endpoints."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.port not in (None, 80, 443):
        raise ValueError("blocked: not a plain web address")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError("blocked: host does not resolve")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise ValueError("blocked: non-public address")


class _PublicOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_PublicOnlyRedirects)


def fetch(url, timeout=15):
    assert_public(url)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _opener.open(req, timeout=timeout) as r:
        return r.read(300_000).decode("utf-8", "ignore"), dict(r.headers)


def check_website(website, fetch_fn=fetch, platform="facebook"):
    """Returns has_shopify | no_store | marketplace_only | unknown."""
    return inspect_website(website, fetch_fn, platform)[0]


def inspect_website(website, fetch_fn=fetch, platform="facebook"):
    """Returns (status, html). html is "" when nothing was fetched."""
    if not website:
        # Nothing linked. On a marketplace that means they only sell there; elsewhere, social-only.
        # Either way, verify by eye before outreach.
        return ("marketplace_only" if platform in MARKETPLACE_PLATFORMS else "no_store"), ""
    if any(m in website.lower() for m in MARKETPLACES):
        return "marketplace_only", ""
    if not re.match(r"https?://", website):
        website = "https://" + website
    try:
        html, headers = fetch_fn(website)
    except (urllib.error.URLError, OSError, ValueError):
        return "unknown", ""
    return classify_html(html, headers), html
