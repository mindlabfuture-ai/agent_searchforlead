"""Lead discovery through the Google Places API (official, paid per request; no scraping).

A Places listing gives a real, operating local business with its address, phone and, when it has one, a website.
That website is often a Facebook page, an Instagram page or a Shopee shop, which then becomes a normal lead of that
platform; an own domain becomes a `web` lead (checked for Shopify, and for a public email); no website at all becomes
a `maps` lead. Google gives no email address, so a `maps` lead is a phone or in-person lead until one is found."""
from datetime import date

from . import config, db, dedupe, search

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join("places." + f for f in ("id", "displayName", "formattedAddress", "websiteUri", "nationalPhoneNumber",
                                          "googleMapsUri", "businessStatus", "primaryTypeDisplayName"))
PER_PAGE = 20


def query(niche, loc):
    return f"{niche} shop in {loc}" if loc.lower() != "philippines" else f"{niche} shop in the Philippines"


def daily_queries(today=None, n=3):
    """Rotate through niche x location, n queries a day, so each day covers something new."""
    base = (today or date.today()).toordinal() * n
    out = []
    for i in range(n):
        k = base + i
        out.append(query(config.NICHES[k % len(config.NICHES)], config.LOCATIONS[(k // len(config.NICHES)) % len(config.LOCATIONS)]))
    return out


def search_places(text, key, page_token=None):
    body = {"textQuery": text, "regionCode": "PH", "languageCode": "en", "pageSize": PER_PAGE}
    if page_token:
        body["pageToken"] = page_token
    data = search._post_json(ENDPOINT, body, {"Content-Type": "application/json", "X-Goog-Api-Key": key,
                                              "X-Goog-FieldMask": FIELDS + ",nextPageToken"})
    return data.get("places", []), data.get("nextPageToken")


def to_lead(place):
    """A Places result as upsert_lead arguments, or None if it is closed or has no name."""
    if place.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
        return None
    name = ((place.get("displayName") or {}).get("text") or "").strip()
    if not name:
        return None
    kind = (place.get("primaryTypeDisplayName") or {}).get("text") or ""
    snippet = " | ".join(x for x in (kind, place.get("formattedAddress"), "Phone " + place["nationalPhoneNumber"]
                                     if place.get("nationalPhoneNumber") else "", "Google Maps listing") if x)
    maps_url = place.get("googleMapsUri") or f"https://www.google.com/maps/place/?q=place_id:{place.get('id', '')}"
    return site_lead(dict(name=name, snippet=snippet, source="places"), (place.get("websiteUri") or "").strip(), maps_url)


def site_lead(lead, site, listing_url):
    """Finish a directory listing as upsert_lead arguments, by what its website field holds. A social or marketplace
    page is an ordinary lead of that platform, an own domain a `web` lead, no site a `maps` lead keyed by the listing."""
    if site:
        platform, canon = db.normalize(site)
        if canon:
            return dict(lead, url=canon)
        host = dedupe.host_key(site)  # None for social/marketplace/link-in-bio/shortener hosts
        if host:
            return dict(lead, url=f"https://{host}", website=f"https://{host}", platform="web")
    return dict(lead, url=listing_url, website=site, platform="maps")


def discover(con, key=None, queries=None, today=None, max_pages=None, log=print, search_fn=None):
    """Run today's queries; returns the number of new leads. A failing query is logged and skipped."""
    key = key or config.env("GOOGLE_PLACES_API_KEY")
    queries = queries if queries is not None else daily_queries(today, config.env_int("PLACES_DAILY_QUERIES", 3))
    max_pages = max_pages or config.env_int("PLACES_MAX_PAGES", 2)
    fetch = search_fn or search_places
    added = 0
    for q in queries:
        token, new = None, 0
        for _ in range(max(1, max_pages)):
            try:
                places, token = fetch(q, key, token)
            except Exception as e:  # a bad key, quota or network error must not stop the daily run
                log(f"! places {q}: {e}")
                break
            for p in places:
                lead = to_lead(p)
                if lead:
                    lead["source"] = f"places:{q}"
                    new += db.upsert_lead(con, **lead)
            if not token:
                break
        added += new
        log(f"{new:>3} new  <- places: {q}")
    return added
