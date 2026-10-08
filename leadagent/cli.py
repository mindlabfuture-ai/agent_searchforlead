import argparse
import csv
import sys

from . import config, db, dedupe, emailing, followups, importer, pipeline, popload, previews, search, showcase


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
    results, truncated = importer.add_rows(con, importer.parse_csv(open(a.file, encoding="utf-8").read()), source="import")
    for label, status, note in results:
        if status != "added" or note:
            print(f"  {status}: {label} {note}")
    print("added", sum(1 for _, s, _ in results if s == "added"), "of", len(results),
          "(only the first %d rows are read)" % importer.MAX_ROWS if truncated else "")
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
    pipeline.check_all(con)


def cmd_score(a, con):
    print(pipeline.score_all(con), "qualified")


def cmd_draft(a, con):
    pipeline.draft_n(con, a.limit)
    print("drafted; run `export` to review")


def cmd_set_email(a, con):
    found = emailing.extract_emails(a.email)
    if not found:
        raise SystemExit("not a usable business email address")
    con.execute("UPDATE leads SET email=?, email_source='added manually' WHERE id=?", (found[0], a.id))
    con.commit()
    print(f"lead #{a.id} email set to {found[0]}")


def cmd_send(a, con):
    """Send approved emails (dry run unless EMAIL_SENDING_ENABLED=true). Approve leads in the dashboard first."""
    n = emailing.run_sender(con, force=a.force)
    live = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    print(n, "sent" if live else "(dry run: nothing sent)")


def cmd_client_add(a, con):
    cid, err = followups.add_client(con, a.name, a.email, a.store_url, a.handed_over, a.lead_id, a.popload)
    if err:
        raise SystemExit(f"could not add: {err}")
    print(f"client #{cid} added; follow-ups scheduled")


def cmd_client_list(a, con):
    for c in con.execute("SELECT * FROM clients ORDER BY id"):
        fu = ", ".join(f"{f['step']}={f['status']}@{f['due_at']}" for f in
                       con.execute("SELECT * FROM followups WHERE client_id=? ORDER BY due_at", (c["id"],)))
        print(f"#{c['id']} {c['name']} <{c['email']}> {c['status']} popload={c['popload_status']}  {fu}")


def cmd_followups(a, con):
    """Send follow-ups that are due (dry run unless EMAIL_SENDING_ENABLED=true)."""
    n = followups.run(con, force=a.force)
    live = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    print(n, "sent" if live else "(dry run: nothing sent)")


def cmd_preview(a, con):
    """Build (or rebuild) the store preview for a lead from its website and anything already uploaded."""
    lead = con.execute("SELECT * FROM leads WHERE id=?", (a.lead_id,)).fetchone()
    if not lead:
        raise SystemExit("unknown lead id")
    pv = previews.generate(con, lead)
    print(f"preview for {pv['name']}: brand {pv['brand']} ({pv['brand_note']}), logo {pv['logo']['kind']}, "
          f"{len(pv['products'])} product(s){' (samples)' if any(p['sample'] for p in pv['products']) else ''}. Review it at /previews/{a.lead_id}.")


def cmd_showcase(a, con):
    """Send approved showcase emails that are due (dry run unless EMAIL_SENDING_ENABLED=true)."""
    n = showcase.run(con, force=a.force)
    print(n, "sent" if config.env("EMAIL_SENDING_ENABLED").lower() == "true" else "(dry run: nothing sent)")


def cmd_prospects(a, con):
    """POPLoad prospects: existing Shopify stores. import FILE | verify | list [--status S] | run [--force]"""
    if a.sub == "import":
        results, _ = popload.add_rows(con, popload.parse_csv(open(a.file, encoding="utf-8").read()))
        print("added", sum(1 for _, s, _ in results if s == "added"), "of", len(results))
        for label, st, note in results:
            if st != "added":
                print(f"  {st}: {label} {note}")
    elif a.sub == "adopt":
        print("adopted", popload.adopt_shopify_leads(con))
    elif a.sub == "discover":
        n = popload.discover(con, search.provider(), popload.discovery_queries(n=a.max_queries))
        print("added", n, "(run `prospects verify` next)")
    elif a.sub == "verify":
        while popload.verify_all(con):
            pass
    elif a.sub == "list":
        for p in con.execute("SELECT * FROM prospects WHERE (?='' OR status=?) ORDER BY id", (a.status, a.status)):
            print(f"#{p['id']} {p['status']:11} {p['name'][:30]:30} {p['domain'][:30]:30} {p['pay_level'] or '-':7} {p['email'] or '-'} {p['reject_reason'] or ''}")
    elif a.sub == "run":
        n = popload.run(con, force=a.force)
        print(n, "sent" if config.env("EMAIL_SENDING_ENABLED").lower() == "true" else "(dry run: nothing sent)")


def cmd_serve(a, con):
    from . import server
    server.serve()


def cmd_export(a, con):
    rows = con.execute("SELECT * FROM leads WHERE score>0 AND status!='merged' ORDER BY score DESC").fetchall()
    cols = ["score", "status", "platform", "name", "url", "email", "also_on", "website", "shopify_status", "score_notes", "draft"]
    w = csv.writer(open(a.out, "w", newline="", encoding="utf-8"))
    w.writerow(cols)
    w.writerows([[r[c] for c in cols] for r in rows])
    print(len(rows), "leads ->", a.out)


def cmd_mark(a, con):
    url = db.normalize(a.url)[1] or a.url
    if a.status == "do_not_contact":
        db.add_do_not_contact(con, url, a.reason)
        row = con.execute("SELECT email FROM leads WHERE url=?", (url,)).fetchone()
        if row and row["email"]:
            emailing.suppress(con, row["email"], a.reason or "marked do not contact")
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
    s = sub.add_parser("set-email", help="record a publicly listed business email for a lead")
    s.add_argument("id", type=int); s.add_argument("email"); s.set_defaults(f=cmd_set_email)
    s = sub.add_parser("send", help="send approved emails now (daily cap applies; dry run unless enabled)")
    s.add_argument("--force", action="store_true", help="ignore the Mon-Fri 9-17 PHT window"); s.set_defaults(f=cmd_send)
    s = sub.add_parser("client", help="clients whose store you handed over"); cs = s.add_subparsers(dest="sub", required=True)
    s = cs.add_parser("add"); s.add_argument("name"); s.add_argument("email"); s.add_argument("--store-url", default="")
    s.add_argument("--handed-over", help="YYYY-MM-DD, default today")
    s.add_argument("--lead-id", type=int); s.add_argument("--popload", default="not_installed", choices=list(followups.POPLOAD_STATUSES))
    s.set_defaults(f=cmd_client_add)
    cs.add_parser("list").set_defaults(f=cmd_client_list)
    s = sub.add_parser("followups", help="send due follow-ups now (dry run unless enabled)")
    s.add_argument("--force", action="store_true", help="ignore the Mon-Fri 9-17 PHT window"); s.set_defaults(f=cmd_followups)
    s = sub.add_parser("preview", help="build a store preview for a lead"); s.add_argument("lead_id", type=int); s.set_defaults(f=cmd_preview)
    s = sub.add_parser("showcase", help="send approved showcase emails that are due (dry run unless enabled)")
    s.add_argument("--force", action="store_true", help="ignore the Mon-Fri 9-17 PHT window"); s.set_defaults(f=cmd_showcase)
    s = sub.add_parser("prospects", help="POPLoad prospects (existing Shopify stores)"); ps = s.add_subparsers(dest="sub", required=True)
    s = ps.add_parser("import"); s.add_argument("file")
    ps.add_parser("verify"); ps.add_parser("adopt")
    s = ps.add_parser("discover"); s.add_argument("--max-queries", type=int, default=4)
    s = ps.add_parser("list"); s.add_argument("--status", default="")
    s = ps.add_parser("run"); s.add_argument("--force", action="store_true")
    s.set_defaults(f=cmd_prospects); ps.choices["import"].set_defaults(f=cmd_prospects); ps.choices["verify"].set_defaults(f=cmd_prospects); ps.choices["adopt"].set_defaults(f=cmd_prospects); ps.choices["discover"].set_defaults(f=cmd_prospects); ps.choices["list"].set_defaults(f=cmd_prospects)
    sub.add_parser("serve", help="run the dashboard + scheduler (Railway)").set_defaults(f=cmd_serve)
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
