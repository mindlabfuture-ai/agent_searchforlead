"""Merge the same business found on several platforms into one lead.

Auto-merge only on strong evidence: same website host, or the same handle on different platforms.
Weaker matches (same business name) are listed for manual review; merge them with `merge ID ID`.
"""
import re
from collections import defaultdict
from urllib.parse import urlparse

from . import config, db

# Higher = further along the pipeline. The most advanced row is kept as the primary.
STATUS_RANK = {s: i for i, s in enumerate(
    ["new", "qualified", "drafted", "approved", "contacted", "replied", "lost", "won", "do_not_contact"])}
# Which store verdict survives a merge. has_shopify must win: one Shopify store disqualifies the business.
SHOPIFY_RANK = {"has_shopify": 5, "unknown": 4, "marketplace_only": 3, "no_store": 2, "unchecked": 1}


def host_key(website):
    if not website:
        return None
    host = urlparse(website if "//" in website else "//" + website).netloc.lower()
    host = re.sub(r"^(www|m)\.", "", host)
    if not host or any(host == g or host.endswith("." + g) for g in config.GENERIC_HOSTS + config.NON_STORE_HOSTS):
        return None
    return host


def handle_key(platform, url):
    """Alphanumeric handle, so glow.ph / glow_ph / GlowPH all collapse to 'glowph'."""
    m = None
    if platform in ("instagram", "tiktok"):
        m = re.search(r"\.com/@?([^/?#]+)$", url)
    elif platform == "facebook":
        m = re.search(r"facebook\.com/([^/?#]+)$", url)
        if m and m.group(1).lower() == "profile.php":
            m = None
    elif platform in ("shopee", "lazada", "carousell"):
        m = re.search(r"/(?:shop/|u/)?([^/?#]+)$", url)
    key = re.sub(r"[^a-z0-9]", "", m.group(1).lower()) if m else ""
    return key if len(key) >= 4 and not key.isdigit() else None


def name_key(name):
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    return key if len(key) >= 6 else None


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def find_matches(rows):
    """Returns (groups, possibles). groups: lists of rows (len>=2) to auto-merge.
    possibles: (row_a, row_b, reason) for human review."""
    by = {r["id"]: r for r in rows}
    uf = _UF()
    buckets = defaultdict(list)
    for r in rows:
        uf.find(r["id"])
        for kind, key in (("website", host_key(r["website"])), ("handle", handle_key(r["platform"], r["url"]))):
            if key:
                buckets[(kind, key)].append(r)
    for (kind, _), members in buckets.items():
        for m in members[1:]:
            # same handle on the SAME platform is impossible (url is unique), so handle matches are cross-platform
            uf.union(members[0]["id"], m["id"])
    groups = defaultdict(list)
    for r in rows:
        groups[uf.find(r["id"])].append(r)
    auto = [g for g in groups.values() if len(g) > 1]

    # weaker evidence, only between different groups
    possibles, seen = [], set()
    names = defaultdict(list)
    for r in rows:
        n, h = name_key(r["name"]), handle_key(r["platform"], r["url"])
        if n:
            names[n].append(r)
        if h and len(h) >= 6 and h != n:  # a business name equal to another profile's handle
            names[h].append(r)
    for key, members in names.items():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if a["id"] == b["id"] or uf.find(a["id"]) == uf.find(b["id"]) or a["platform"] == b["platform"]:
                    continue
                pair = tuple(sorted((a["id"], b["id"])))
                if pair not in seen:
                    seen.add(pair)
                    possibles.append((a, b, f"same name/handle '{key}'"))
    return auto, possibles


def pick_primary(rows):
    return sorted(rows, key=lambda r: (-STATUS_RANK.get(r["status"], 0), 0 if r["website"] else 1, r["id"]))[0]


def merge_rows(con, rows):
    """Fold `rows` into one lead. Returns the primary row id."""
    primary = pick_primary(rows)
    others = [r for r in rows if r["id"] != primary["id"]]
    snippets, seen = [], set()
    for r in [primary] + others:
        for part in (r["snippet"] or "").split(" | "):
            if part and part not in seen:
                seen.add(part); snippets.append(part)
    website = primary["website"] or next((r["website"] for r in others if r["website"]), "")
    also = [x.strip() for x in (primary["also_on"] or "").split(";") if x.strip()]
    for r in others:
        also.append(f"{r['platform']}:{r['url']}")
        also += [x.strip() for x in (r["also_on"] or "").split(";") if x.strip()]
    also = list(dict.fromkeys(also))
    verdict = max((r["shopify_status"] for r in rows), key=lambda s: SHOPIFY_RANK.get(s, 0))
    status = primary["status"]
    if verdict != "has_shopify" and website != primary["website"]:
        verdict = "unchecked"  # we adopted a new website: re-run `check` on it
    if status == "qualified":
        status = "new"  # re-evaluate with the combined evidence
    t = db.now()
    donor = next((r for r in [primary] + others if r["email"]), None)
    con.execute("UPDATE leads SET snippet=?, website=?, also_on=?, shopify_status=?, status=?, email=?, email_source=?, updated_at=? WHERE id=?",
                (" | ".join(snippets), website, "; ".join(also), verdict, status,
                 donor["email"] if donor else None, donor["email_source"] if donor else None, t, primary["id"]))
    for r in others:
        con.execute("UPDATE leads SET status='merged', merged_into=?, score=0, updated_at=? WHERE id=?",
                    (primary["id"], t, r["id"]))
    if primary["status"] == "do_not_contact":  # an opt-out on one profile covers the whole business
        for r in rows:
            db.add_do_not_contact(con, r["url"], "merged with an opted-out profile")
        con.execute("UPDATE leads SET status='merged' WHERE merged_into=?", (primary["id"],))  # add_do_not_contact flips status
    con.commit()
    return primary["id"]


def run(con, apply=True, log=print):
    """Auto-merge strong matches, list weak ones. Returns (merged_groups, possibles)."""
    rows = con.execute("SELECT * FROM leads WHERE status!='merged'").fetchall()
    groups, possibles = find_matches(rows)
    for g in groups:
        label = " + ".join(f"{r['platform']}:{r['name'] or r['url']}" for r in g)
        log(("merged  " if apply else "would merge  ") + label)
        if apply:
            merge_rows(con, g)
    for a, b, why in possibles:
        log(f"review  [{a['id']}] {a['platform']} {a['name']}  ~  [{b['id']}] {b['platform']} {b['name']}  ({why})"
            f"  -> leadagent merge {a['id']} {b['id']}")
    return groups, possibles
