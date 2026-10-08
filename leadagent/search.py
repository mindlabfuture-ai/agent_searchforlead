"""Lead discovery through search APIs (no Facebook scraping)."""
import itertools
import urllib.parse
import json
import re
import urllib.request

from . import config
from .db import normalize_fb_url

URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def build_queries(niches=None, locations=None):
    for niche, loc, tpl in itertools.product(niches or config.NICHES,
                                             locations or config.LOCATIONS,
                                             config.QUERY_TEMPLATES):
        yield tpl.format(niche=niche, loc=loc)


def _post_json(url, payload, headers):
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def serper(query, key, num=20):
    data = _post_json("https://google.serper.dev/search", {"q": query, "num": num, "gl": "ph"},
                      {"X-API-KEY": key, "Content-Type": "application/json"})
    return [{"url": o.get("link", ""), "title": o.get("title", ""), "snippet": o.get("snippet", "")}
            for o in data.get("organic", [])]


def brave(query, key, num=20):
    q = urllib.parse.quote(query)
    data = _get_json(f"https://api.search.brave.com/res/v1/web/search?q={q}&count={num}&country=ph",
                     {"X-Subscription-Token": key, "Accept": "application/json"})
    return [{"url": o.get("url", ""), "title": o.get("title", ""), "snippet": o.get("description", "")}
            for o in data.get("web", {}).get("results", [])]


def provider():
    if config.env("SERPER_API_KEY"):
        return lambda q: serper(q, config.env("SERPER_API_KEY"))
    if config.env("BRAVE_API_KEY"):
        return lambda q: brave(q, config.env("BRAVE_API_KEY"))
    raise SystemExit("Set SERPER_API_KEY or BRAVE_API_KEY (see .env.example), or use `import`.")


def extract_website(text):
    """First non-social/marketplace URL mentioned in text."""
    for u in URL_RE.findall(text or ""):
        host = re.sub(r"^https?://(www\.)?", "", u.lower()).split("/")[0]
        if not any(h in host for h in config.NON_STORE_HOSTS):
            return u.rstrip(".,")
    return ""


def clean_name(title):
    return re.sub(r"\s*[-|·]\s*(Home|Facebook|Posts|Photos|About).*$", "", title or "", flags=re.I).strip()


def results_to_leads(results):
    for r in results:
        url = normalize_fb_url(r["url"])
        if url:
            yield {"fb_url": url, "name": clean_name(r["title"]), "snippet": r["snippet"],
                   "website": extract_website(r["snippet"])}


def run(con, queries, search_fn, max_queries=None, log=print):
    from .db import upsert_lead
    added = 0
    for i, q in enumerate(queries):
        if max_queries is not None and i >= max_queries:
            break
        try:
            results = search_fn(q)
        except Exception as e:  # keep going on a bad query / rate limit
            log(f"! {q}: {e}")
            continue
        n = sum(upsert_lead(con, source=f"search:{q}", **l) for l in results_to_leads(results))
        added += n
        log(f"{n:>3} new  <- {q}")
    return added


def meta_graph_search(query, token, limit=25):
    """Optional official route. Needs the 'Page Public Content Access' feature (Meta app review)."""
    import urllib.parse
    qs = urllib.parse.urlencode({"q": query, "fields": "name,link,about,website,location",
                                 "limit": limit, "access_token": token})
    data = _get_json(f"https://graph.facebook.com/v21.0/pages/search?{qs}", {})
    return [{"url": p.get("link", ""), "title": p.get("name", ""),
             "snippet": " ".join(filter(None, [p.get("about"), p.get("website"),
                                               (p.get("location") or {}).get("city")]))}
            for p in data.get("data", [])]
