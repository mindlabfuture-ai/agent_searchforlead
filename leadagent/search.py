"""Lead discovery through search APIs (no Facebook scraping)."""
import itertools
import urllib.error
import urllib.parse
import json
import re
import urllib.request

from . import config
from .db import normalize

URL_RE = re.compile(r"https?://[^\s\"'<>)]+")


def build_queries(platform="facebook", niches=None, locations=None):
    for niche, loc, tpl in itertools.product(niches or config.NICHES,
                                             locations or config.LOCATIONS,
                                             config.PLATFORM_QUERIES[platform]):
        yield tpl.format(niche=niche, loc=loc)


class SearchError(Exception):
    """A search API refused a query. Carries the HTTP status and the start of the reply, so the log says why."""
    def __init__(self, status, body=""):
        super().__init__(f"HTTP {status}: {body}".strip())
        self.status = status


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            body = ""
        raise SearchError(e.code, body) from e


def _post_json(url, payload, headers):
    return _open(urllib.request.Request(url, json.dumps(payload).encode(), headers, method="POST"))


def _get_json(url, headers):
    return _open(urllib.request.Request(url, headers=headers))


def search_once(search_fn, query):
    """Run a query. If the provider answers 400 to one with quote marks, try again without them: quoted phrases are a
    nicety, and a refused query would otherwise return nothing at all."""
    try:
        return search_fn(query)
    except SearchError as e:
        if e.status == 400 and '"' in query:
            return search_fn(" ".join(query.replace('"', " ").split()))
        raise


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
    t = re.sub(r"\s*[-|·]\s*(Home|Facebook|Posts|Photos|About|Shopee|Lazada|Carousell|TikTok).*$", "", title or "", flags=re.I)
    return re.sub(r"\s*\(@[\w.]+\).*$", "", t).strip()  # "Name (@handle) • Instagram photos..."


def results_to_leads(results):
    for r in results:
        _, url = normalize(r["url"])
        if url:
            yield {"url": url, "name": clean_name(r["title"]), "snippet": r["snippet"],
                   "website": extract_website(r["snippet"])}


def run(con, queries, search_fn, max_queries=None, log=print):
    """Returns (new_leads, successful_queries). successful==0 means the source looks unavailable."""
    from .db import upsert_lead
    added = ok = 0
    for i, q in enumerate(queries):
        if max_queries is not None and i >= max_queries:
            break
        try:
            results = search_once(search_fn, q)
        except Exception as e:  # keep going on a bad query / rate limit
            log(f"! {q}: {e}")
            continue
        ok += 1
        n = sum(upsert_lead(con, source=f"search:{q}", **l) for l in results_to_leads(results))
        added += n
        log(f"{n:>3} new  <- {q}")
    return added, ok


def run_auto(con, search_fn, niche=None, location=None, max_queries=10, min_new=5, log=print):
    """Facebook first; if it is down or yields < min_new new leads, fall back to other platforms in order."""
    total = 0
    for platform in config.PLATFORM_QUERIES:
        qs = list(build_queries(platform, [niche] if niche else None, [location] if location else None))
        log(f"== {platform}: {len(qs)} queries, running up to {max_queries}")
        added, ok = run(con, qs, search_fn, max_queries, log)
        total += added
        if ok == 0:
            log(f"   {platform} unavailable (0 successful queries) -> falling back")
        if total >= min_new:
            break
    return total


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
