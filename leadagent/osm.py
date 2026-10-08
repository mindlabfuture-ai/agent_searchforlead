"""Free lead discovery from OpenStreetMap (Overpass API): no key, no account, no prepayment.

Shops that mappers tagged with a website, Facebook/Instagram page, email or phone. A listing becomes a lead by what it
carries, the same way as Google Places (see places.site_lead); a published `email` tag is kept as the lead's public
email. Coverage of small Philippine online sellers is thinner than Google's, so treat it as a steady trickle, not a flood.
Data (c) OpenStreetMap contributors, ODbL. Be polite to the shared server: few queries, a pause between them."""
import time
from datetime import date

from . import config, emailing, places, search

GROUPS = {  # a rotation of shop types that match what the agent sells for
    "fashion": "clothes|shoes|bag|jewelry|boutique|fashion_accessories|watches|tailor|fabric",
    "beauty": "cosmetics|beauty|perfumery|massage|herbalist",
    "home": "houseware|candles|interior_decoration|furniture|florist|garden_centre|doityourself",
    "food": "bakery|confectionery|coffee|tea|deli|chocolate|food|health_food|farm|butcher|seafood",
    "kids_pets_gifts": "baby_goods|pet|toys|gift|craft|art|second_hand|variety_store|stationery",
    "gadgets": "electronics|mobile_phone|computer|hifi|video_games|sports|bicycle",
}
CONTACT_TAGS = ("website", "contact:website", "contact:facebook", "contact:instagram", "email", "contact:email")
LINK_ORDER = ("website", "contact:website", "contact:facebook", "contact:instagram")
MAX_ELEMENTS = 150


def area_clause(loc):
    if loc.lower() == "philippines":
        return 'area["ISO3166-1"="PH"]["boundary"="administrative"]->.a;'
    name = loc.replace('"', "")
    return f'area["name"="{name}"]["boundary"="administrative"]->.a;'


def build_query(group, loc, limit=MAX_ELEMENTS):
    tests = "".join(f'nwr["shop"~"^({GROUPS[group]})$"]["{t}"~"."](area.a);' for t in CONTACT_TAGS)
    return f"[out:json][timeout:90];{area_clause(loc)}({tests});out tags center {limit};"


def daily_queries(today=None, n=3):
    """(group, location) pairs, n a day, rotating so each day covers something new."""
    groups, locs = list(GROUPS), [l for l in config.LOCATIONS if l.lower() != "philippines"]
    base = (today or date.today()).toordinal() * n
    return [(groups[(base + i) % len(groups)], locs[((base + i) // len(groups)) % len(locs)]) for i in range(n)]


ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]


def overpass(query, endpoints=None, post=None):
    """Elements from the first public Overpass server that answers properly. These shared servers are often busy, and
    some answer HTTP 200 with a `remark` about a runtime error, so each is tried in turn before giving up."""
    urls = endpoints or [u for u in config.env("OVERPASS_URL").split(",") if u.strip()] or ENDPOINTS
    post = post or (lambda url, body: search._open(search.urllib.request.Request(
        url, body, {"User-Agent": "leadagent/1.0 (+https://mindlabfuture-ai.com)"}, method="POST")))
    body = search.urllib.parse.urlencode({"data": query}).encode()
    last = None
    for url in urls:
        try:
            data = post(url.strip(), body)
        except Exception as e:
            last = e
            continue
        if data.get("remark") and not data.get("elements"):
            last = RuntimeError(str(data["remark"])[:200])
            continue
        return data.get("elements", [])
    raise last or RuntimeError("no Overpass server configured")


def _link(tags):
    for key in LINK_ORDER:
        v = (tags.get(key) or "").strip()
        if not v:
            continue
        if v.startswith(("http://", "https://")) or "." in v.split("/")[0]:
            return v
        if key == "contact:facebook":
            return "https://facebook.com/" + v.lstrip("@/")
        if key == "contact:instagram":
            return "https://instagram.com/" + v.lstrip("@/")
    return ""


def to_lead(el):
    tags = el.get("tags") or {}
    name = (tags.get("name") or "").strip()
    if not name or tags.get("disused:shop") or tags.get("abandoned"):
        return None
    where = ", ".join(x for x in (tags.get("addr:street"), tags.get("addr:city") or tags.get("addr:suburb")) if x)
    snippet = " | ".join(x for x in (tags.get("shop", "").replace("_", " ").title() + " shop", where,
                                     "Phone " + (tags.get("phone") or tags.get("contact:phone") or "") if (tags.get("phone") or tags.get("contact:phone")) else "",
                                     "OpenStreetMap listing") if x)
    lead = places.site_lead(dict(name=name, snippet=snippet, source="osm"), _link(tags),
                            f"https://www.openstreetmap.org/{el.get('type', 'node')}/{el.get('id')}")
    found = emailing.extract_emails(tags.get("email") or tags.get("contact:email") or "")
    if found:
        lead.update(email=found[0], email_source="its OpenStreetMap listing")
    return lead


def discover(con, queries=None, today=None, log=print, fetch=None, pause=3):
    """Run today's queries; returns the number of new leads. A failing query is logged and skipped."""
    from .db import upsert_lead
    queries = queries if queries is not None else daily_queries(today, config.env_int("OSM_DAILY_QUERIES", 3))
    fetch = fetch or overpass
    added = 0
    for i, (group, loc) in enumerate(queries):
        if i and pause:
            time.sleep(pause)
        try:
            elements = fetch(build_query(group, loc))
        except Exception as e:  # a busy public server or a bad area name must not stop the daily run
            log(f"! osm {group}/{loc}: {e}")
            continue
        new = 0
        for el in elements:
            lead = to_lead(el)
            if lead:
                lead["source"] = f"osm:{group}/{loc}"
                new += upsert_lead(con, **lead)
        added += new
        log(f"{new:>3} new  <- osm: {group} / {loc} ({len(elements)} listings)")
    return added
