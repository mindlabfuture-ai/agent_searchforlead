import argparse
import csv
import sys

from . import config, db, dedupe, outreach, scoring, search, shopify_check


def load_env():
    import os
    if os.path.exists(".env"):
        for line in open(".env"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def cmd_search(a, con):
    fn = search.provider()
    if a.platform == "auto":
        print("added", search.run_auto(con, fn, a.niche, a.location, a.max_queries, a.min_new))
        dedupe.run(con)
        return
    qs = list(search.build_queries(a.platform, [a.niche] if a.niche else None,
                                   [a.location] if a.location else None))
    print(f"{len(qs)} queries ({a.max_queries} will run)")
    added, ok = search.run(con, qs, fn, a.max_queries)
    print("added", added)
    dedupe.run(con)
    if ok == 0:
        print(f"{a.platform} returned nothing; try `--platform auto` to fall back.")


def cmd_import(a, con):
    """CSV columns: url,name,snippet,website (any supported platform; `fb_url` also accepted)."""
    n = 0
    for row in csv.DictReader(open(a.file, newline="", encoding="utf-8")):
        n += db.upsert_lead(con, row.get("url") or row["fb_url"], row.get("name", ""), row.get("snippet", ""),
                            row.get("website", ""), source="import")
    print("added", n)
    dedupe.run(con)


def cmd_dedupe(a, con):
    groups, possibles = dedupe.run(con, apply=not a.dry_run)
    print(f"{len(groups)} duplicate group(s) {'found' if a.dry_run else 'merged'}, {len(possibles)} to review")
    if groups and not a.dry_run:
        print("Run `check` then `score` to refresh the merged leads.")


def cmd_merge(a, con):
    rows = [con.execute("SELECT * FROM leads WHERE id=? AND status!='merged'", (i,)).fetchone() for i in a.ids]
    if any(r is None for r in rows):
        raise SystemExit("unknown or already-merged lead id")
    pid = dedupe.merge_rows(con, rows)
    print(f"merged {len(rows)} leads into #{pid}")


def cmd_check(a, con):
    rows = con.execute("SELECT * FROM leads WHERE shopify_status='unchecked' AND status='new'").fetchall()
    for r in rows:
        s = shopify_check.check_website(r["website"], platform=r["platform"])
        con.execute("UPDATE leads SET shopify_status=?, updated_at=? WHERE id=?", (s, db.now(), r["id"]))
        print(f"{s:<17} [{r['platform']}] {r['name'] or r['url']}")
    con.commit()


def cmd_score(a, con):
    for r in con.execute("SELECT * FROM leads WHERE status IN ('new','qualified')").fetchall():
        sc, notes = scoring.score_lead(r)
        st = "qualified" if sc >= scoring.QUALIFY_AT else "new"
        con.execute("UPDATE leads SET score=?, score_notes=?, status=?, updated_at=? WHERE id=?",
                    (sc, notes, st, db.now(), r["id"]))
    con.commit()
    print(con.execute("SELECT COUNT(*) FROM leads WHERE status='qualified'").fetchone()[0], "qualified")


def cmd_draft(a, con):
    for r in con.execute("SELECT * FROM leads WHERE status='qualified' ORDER BY score DESC LIMIT ?", (a.limit,)):
        con.execute("UPDATE leads SET draft=?, status='drafted', updated_at=? WHERE id=?",
                    (outreach.draft(r), db.now(), r["id"]))
    con.commit()
    print("drafted; run `export` to review")


def cmd_export(a, con):
    rows = con.execute("SELECT * FROM leads WHERE score>0 AND status!='merged' ORDER BY score DESC").fetchall()
    cols = ["score", "status", "platform", "name", "url", "also_on", "website", "shopify_status", "score_notes", "draft"]
    w = csv.writer(open(a.out, "w", newline="", encoding="utf-8"))
    w.writerow(cols)
    w.writerows([[r[c] for c in cols] for r in rows])
    print(len(rows), "leads ->", a.out)


def cmd_mark(a, con):
    url = db.normalize(a.url)[1] or a.url
    if a.status == "do_not_contact":
        db.add_do_not_contact(con, url, a.reason)
    else:
        con.execute("UPDATE leads SET status=?, updated_at=? WHERE url=?", (a.status, db.now(), url))
        con.commit()
    print(url, "->", a.status)


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="leadagent")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("search"); s.add_argument("--niche"); s.add_argument("--location")
    s.add_argument("--platform", default="auto", choices=["auto"] + db.PLATFORMS,
                   help="auto = Facebook first, then fall back to other platforms")
    s.add_argument("--min-new", type=int, default=5, help="auto: keep falling back until this many new leads")
    s.add_argument("--max-queries", type=int, default=10); s.set_defaults(f=cmd_search)
    s = sub.add_parser("import"); s.add_argument("file"); s.set_defaults(f=cmd_import)
    s = sub.add_parser("dedupe", help="merge the same business found on several platforms")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(f=cmd_dedupe)
    s = sub.add_parser("merge", help="manually merge leads by id (see `dedupe` review lines)")
    s.add_argument("ids", type=int, nargs="+"); s.set_defaults(f=cmd_merge)
    sub.add_parser("check").set_defaults(f=cmd_check)
    sub.add_parser("score").set_defaults(f=cmd_score)
    s = sub.add_parser("draft"); s.add_argument("--limit", type=int, default=25); s.set_defaults(f=cmd_draft)
    s = sub.add_parser("export"); s.add_argument("--out", default="data/leads.csv"); s.set_defaults(f=cmd_export)
    s = sub.add_parser("mark"); s.add_argument("url")
    s.add_argument("status", choices=["contacted", "replied", "won", "lost", "do_not_contact"])
    s.add_argument("--reason", default=""); s.set_defaults(f=cmd_mark)
    a = p.parse_args(argv)
    a.f(a, db.connect(config.DB_PATH))


if __name__ == "__main__":
    sys.exit(main())
