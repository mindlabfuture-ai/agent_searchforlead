"""The owner's right-hand assistant: it watches the whole system, says what needs attention, and queues suggested
actions for one-click confirmation. Everything here is plain code (no model): `assistantchat.py` puts a conversation on top.

Rules that are enforced in code, not left to a prompt:
- It can stop, pause, close or tidy things. It can never start outreach: there is no action here that approves a
  first email, a showcase email, a POPLoad sequence or a demo site. Those stay with you, one by one.
- A suggested action only runs when you confirm it in the dashboard.
- It never sends mail to leads. The only email it can send is the daily brief to your own OWNER_EMAIL."""
import json
from datetime import datetime, timedelta, timezone

from . import config, db, demosite, emailing, followups, inbox, popload, previewui, showcase

CRITICAL, WARN, INFO = "critical", "warn", "info"
RANK = {CRITICAL: 0, WARN: 1, INFO: 2}
BOUNCE_WARN, BOUNCE_CRIT = 0.04, 0.08          # Resend's own guidance is to stay under about 4% bounces
COMPLAINT_WARN, COMPLAINT_CRIT = 0.001, 0.003  # and under about 0.1% spam complaints
MIN_SAMPLE = 20


def _now(now=None):
    return now or datetime.now(timezone.utc)


def _utc(ts):
    d = datetime.fromisoformat(ts)
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d


def finding(severity, key, title, detail="", fix=""):
    return {"severity": severity, "key": key, "title": title, "detail": detail, "fix": fix}


# ---------------- health ----------------
def health(con, now=None):
    """A list of findings, worst first. Only reads the database and the environment."""
    now = _now(now)
    out = []
    sending = config.env("EMAIL_SENDING_ENABLED").lower() == "true"
    if len(config.env("UNSUB_SECRET")) < 16:
        out.append(finding(CRITICAL, "unsub_secret", "UNSUB_SECRET is missing or too short",
                           "Unsubscribe and image links cannot be signed.", "Set UNSUB_SECRET (32+ characters) in Railway."))
    if not config.base_url():
        out.append(finding(CRITICAL, "base_url", "BASE_URL is not set", "Emails need it for the unsubscribe link.",
                           "Set BASE_URL to https://leads.mindlabfuture-ai.com in Railway."))
    if sending:
        for var, why in (("RESEND_API_KEY", "nothing can be sent"), ("SENDER_FROM_EMAIL", "nothing can be sent"),
                         ("RESEND_WEBHOOK_SECRET", "bounces and spam complaints will not be suppressed")):
            if not config.env(var):
                out.append(finding(CRITICAL, var.lower(), f"{var} is not set while sending is on", why, f"Set {var} in Railway."))
    else:
        out.append(finding(INFO, "dry_run", "Sending is off", "Everything runs as a dry run.", "Set EMAIL_SENDING_ENABLED=true in Railway when ready."))
    if not (config.env("SERPER_API_KEY") or config.env("BRAVE_API_KEY")):
        out.append(finding(INFO, "no_search", "No search API key", "Daily lead and prospect discovery is off.", "Set SERPER_API_KEY or BRAVE_API_KEY."))

    since = (now - timedelta(days=30)).isoformat(timespec="seconds")
    rows = con.execute("SELECT status, COUNT(*) n FROM emails WHERE sent_at>=? AND status!='failed' GROUP BY status", (since,)).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    total = sum(counts.values())
    if total >= MIN_SAMPLE:
        bounce, complaint = counts.get("bounced", 0) / total, counts.get("complained", 0) / total
        if complaint >= COMPLAINT_CRIT or bounce >= BOUNCE_CRIT:
            out.append(finding(CRITICAL, "deliverability", f"Deliverability is at risk: {bounce:.1%} bounced, {complaint:.2%} complained (last 30 days)",
                               "Resend can suspend accounts above these rates.", "Pause sending, check the lists you are emailing, and lower EMAIL_DAILY_CAP."))
        elif complaint >= COMPLAINT_WARN or bounce >= BOUNCE_WARN:
            out.append(finding(WARN, "deliverability", f"Bounce or complaint rate is climbing: {bounce:.1%} bounced, {complaint:.2%} complained",
                               "", "Slow down and review who is being emailed."))
    day_ago = (now - timedelta(days=1)).isoformat(timespec="seconds")
    failed = con.execute("SELECT COUNT(*) FROM emails WHERE status='failed' AND sent_at>=?", (day_ago,)).fetchone()[0]
    failed += con.execute("SELECT COUNT(*) FROM followups WHERE status='failed'").fetchone()[0]
    failed += con.execute("SELECT COUNT(*) FROM prospect_steps WHERE status='failed'").fetchone()[0]
    if failed:
        out.append(finding(WARN, "failed_sends", f"{failed} email(s) failed to send", "", "Check the Resend dashboard and the Railway logs."))
    old_sent = con.execute("SELECT COUNT(*) FROM emails WHERE status!='failed' AND sent_at<? AND sent_at>=?", (day_ago, since)).fetchone()[0]
    with_events = con.execute("SELECT COUNT(*) FROM emails WHERE status IN ('delivered','bounced','complained')").fetchone()[0]
    if sending and old_sent >= 5 and with_events == 0:
        out.append(finding(WARN, "webhook_silent", "No delivery events have ever arrived from Resend",
                           "The webhook may not be set up, so bounces would not be suppressed.", "Check the Resend webhook URL and RESEND_WEBHOOK_SECRET."))
    last = con.execute("SELECT value FROM meta WHERE key='last_pipeline'").fetchone()
    if (config.env("SERPER_API_KEY") or config.env("BRAVE_API_KEY")) and last:
        age = (now.astimezone(emailing.PHT).date() - datetime.fromisoformat(last[0]).date()).days
        if age >= 2:
            out.append(finding(WARN, "pipeline_stale", f"The daily search has not run for {age} days", "", "Check the Railway logs."))
    live = con.execute("SELECT * FROM demo_sites WHERE status='live'").fetchall()
    if live and not config.env("NETLIFY_AUTH_TOKEN"):
        out.append(finding(CRITICAL, "demo_no_token", f"{len(live)} demo site(s) are live but NETLIFY_AUTH_TOKEN is missing",
                           "They cannot be taken down automatically.", "Set NETLIFY_AUTH_TOKEN in Railway."))
    soon = [d for d in live if _utc(d["expires_at"]) <= now + timedelta(days=3)]
    if soon:
        out.append(finding(INFO, "demo_expiring", f"{len(soon)} demo site(s) expire within 3 days", "", "Extend them on the Previews page if still needed."))
    out += inbox_health(con, now)
    out.sort(key=lambda f: RANK[f["severity"]])
    return out


def inbox_health(con, now):
    out = []
    if config.env("INBOX_ENABLED").lower() == "true":
        missing = [v for v in ("MAIL_PASSWORD", "ANTHROPIC_API_KEY") if not config.env(v)]
        if missing:
            return [finding(CRITICAL, "inbox_config", f"The inbox agent is on but {', '.join(missing)} is not set",
                            "support@ is not being read.", f"Set {', '.join(missing)} in Railway.")]
        ok = con.execute("SELECT value FROM meta WHERE key='inbox_last_ok'").fetchone()
        err = con.execute("SELECT value FROM meta WHERE key='inbox_last_error'").fetchone()
        if not ok or _utc(ok[0]) < now - timedelta(minutes=15):
            out.append(finding(CRITICAL, "inbox_stalled", "The support@ mailbox has not been read for 15 minutes" if ok else "The support@ mailbox has not been read yet",
                               (err[0] if err else "No error recorded."), "Check MAIL_USER, MAIL_PASSWORD (an app password) and IMAP_HOST, then the Railway logs."))
        if not (config.env("TELEGRAM_BOT_TOKEN") and config.env("TELEGRAM_CHAT_ID")):
            out.append(finding(INFO, "inbox_no_telegram", "Telegram is not set up for the inbox", "Alerts and Send buttons only appear in the dashboard.", ""))
    elif config.env("MAIL_PASSWORD"):
        out.append(finding(INFO, "inbox_off", "The inbox agent is off", "A mailbox password is set but INBOX_ENABLED is not true.",
                           "Stop the old standalone agent first, then set INBOX_ENABLED=true."))
    old = con.execute("SELECT COUNT(*) FROM inbox_pending WHERE status='waiting' AND ts<?", ((now - timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchone()[0]
    if old:
        out.append(finding(WARN, "inbox_old_drafts", f"{old} reply draft(s) have waited more than a day", "People who wrote to you are waiting.", "Open the Inbox page."))
    bad = con.execute("SELECT COUNT(*) FROM inbox_messages WHERE status='error' AND attempts>=? AND ts>=?", (inbox.MAX_ATTEMPTS, (now - timedelta(days=7)).isoformat(timespec="seconds"))).fetchone()[0]
    if bad:
        out.append(finding(WARN, "inbox_errors", f"{bad} message(s) could not be read by the agent", "Read them yourself in the mailbox.", "Check the Railway logs."))
    return out


# ---------------- what needs the owner ----------------
def attention(con, now=None):
    """[(label, count, link)] for things only the owner can decide. Nothing here changes data."""
    now = _now(now)
    q = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    items = [
        ("first emails waiting for your approval", q("SELECT COUNT(*) FROM leads WHERE status IN ('qualified','drafted')"), "/"),
        ("approved first emails waiting to be sent", q("SELECT COUNT(*) FROM leads WHERE status='approved'"), "/"),
        ("verified POPLoad prospects waiting for your approval", q("SELECT COUNT(*) FROM prospects WHERE status='verified'"), "/prospects?show=verified"),
        ("prospects that need an email you find yourself", q("SELECT COUNT(*) FROM prospects WHERE status='needs_email'"), "/prospects?show=needs_email"),
        ("failed follow-up emails", q("SELECT COUNT(*) FROM followups WHERE status='failed'"), "/clients"),
    ]
    items.append(("reply drafts waiting for your Send (people who wrote to you)", q("SELECT COUNT(*) FROM inbox_pending WHERE status='waiting'"), "/inbox"))
    ready = 0  # previews built for leads whose day-7 email is close, but not approved yet
    for lead in con.execute("SELECT l.* FROM leads l JOIN previews p ON p.lead_id=l.id WHERE l.status='contacted' AND p.status='draft'"):
        d = showcase.days_since_first(con, lead["id"], now)
        ready += d is not None and d >= showcase.DAYS_UNTIL_SHOWCASE - 2
    items.append(("showcase previews to review and approve (day 7 is near)", ready, "/previews"))
    # a sequence runs by itself, so replies must be marked before the next step goes out
    seq = 0
    for p in con.execute("SELECT id FROM prospects WHERE status='approved'"):
        seq += bool(popload.due_steps(con, {"id": p["id"]}, now + timedelta(days=1))[0])
    items.append(("prospects whose next email goes out within a day: check your inbox for replies first and mark them", seq, "/prospects?show=approved"))
    return [i for i in items if i[1]]


def stats(con, now=None):
    now = _now(now)
    one = lambda sql, *a: con.execute(sql, a).fetchone()[0]
    return {
        "leads": dict(con.execute("SELECT status, COUNT(*) FROM leads WHERE status!='merged' GROUP BY status").fetchall()),
        "prospects": dict(con.execute("SELECT status, COUNT(*) FROM prospects GROUP BY status").fetchall()),
        "clients_active": one("SELECT COUNT(*) FROM clients WHERE status='active'"),
        "emails_today": emailing.sent_today(con, now), "email_cap": config.env_int("EMAIL_DAILY_CAP", 20),
        "emails_30d": one("SELECT COUNT(*) FROM emails WHERE status!='failed' AND sent_at>=?", (now - timedelta(days=30)).isoformat(timespec="seconds")),
        "suppressed": one("SELECT COUNT(*) FROM suppressed_emails"),
        "demos_live": one("SELECT COUNT(*) FROM demo_sites WHERE status='live'"),
        "inbox_24h": dict(con.execute("SELECT COALESCE(category,status), COUNT(*) FROM inbox_messages WHERE ts>=? GROUP BY 1", ((now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchall()),
        "sending_on": config.env("EMAIL_SENDING_ENABLED").lower() == "true",
    }


def pending_proposals(con):
    return con.execute("SELECT * FROM assistant_proposals WHERE status='pending' ORDER BY id").fetchall()


def briefing(con, now=None):
    now = _now(now)
    f, a = health(con, now), attention(con, now)
    crit = sum(x["severity"] == CRITICAL for x in f)
    head = (f"{crit} critical issue(s). " if crit else "") + (f"{sum(c for _, c, _ in a)} thing(s) need you." if a else "Nothing needs you right now.")
    return {"day": now.astimezone(emailing.PHT).date().isoformat(), "headline": head.strip(), "health": f, "attention": [list(x) for x in a],
            "stats": stats(con, now), "proposals": len(pending_proposals(con))}


def render_text(b, base_url=""):
    lines = [f"MindLab daily brief, {b['day']}", b["headline"], ""]
    if b["health"]:
        lines.append("Health")
        lines += [f"- [{x['severity'].upper()}] {x['title']}" + (f" -> {x['fix']}" if x["fix"] else "") for x in b["health"] if x["severity"] != INFO]
        lines += [f"- {x['title']}" for x in b["health"] if x["severity"] == INFO]
        lines.append("")
    if b["attention"]:
        lines.append("Needs you")
        lines += [f"- {n} {label}" + (f" ({base_url}{link})" if base_url else "") for label, n, link in b["attention"]]
        lines.append("")
    s = b["stats"]
    lines.append(f"Emails today {s['emails_today']}/{s['email_cap']}, last 30 days {s['emails_30d']}, suppressed {s['suppressed']}, "
                 f"active clients {s['clients_active']}, live demos {s['demos_live']}. Sending is {'ON' if s['sending_on'] else 'OFF (dry run)'}.")
    inb = s.get("inbox_24h") or {}
    if inb:
        lines.append("Inbox, last 24 hours: " + ", ".join(f"{n} {k}" for k, n in sorted(inb.items())) + ".")
    if b["proposals"]:
        lines.append(f"{b['proposals']} suggested action(s) are waiting for your confirmation in the dashboard.")
    return "\n".join(lines)


def store_and_send(con, now=None, post=None, enabled_owner=None, log=print):
    """Once a day: save the brief and email it to OWNER_EMAIL if that is set. Returns the brief."""
    now = _now(now)
    b = briefing(con, now)
    already = con.execute("SELECT emailed_at FROM assistant_briefings WHERE day=?", (b["day"],)).fetchone()
    con.execute("INSERT OR REPLACE INTO assistant_briefings (day,payload,emailed_at) VALUES (?,?,?)",
                (b["day"], json.dumps(b), already["emailed_at"] if already else None))
    con.commit()
    owner = enabled_owner if enabled_owner is not None else config.env("OWNER_EMAIL")
    if owner and not (already and already["emailed_at"]) and config.env("RESEND_API_KEY") and config.env("SENDER_FROM_EMAIL"):
        base = config.base_url()
        text = render_text(b, base)
        subject = f"MindLab brief: {b['headline']}"
        payload = {"from": f"MindLab Assistant <{config.env('SENDER_FROM_EMAIL')}>", "to": [owner], "subject": subject, "text": text}
        try:
            (post or (lambda p, k: emailing.resend_post(p, config.env("RESEND_API_KEY"), k)))(payload, f"brief-{b['day']}")
            con.execute("UPDATE assistant_briefings SET emailed_at=? WHERE day=?", (db.now(), b["day"]))
            con.commit()
            log(f"daily brief sent to {owner}")
        except Exception as e:  # a failed brief must never stop the scheduler
            log(f"daily brief failed: {e!r}")
    return b


def alert_critical(con, now=None, post=None, log=print):
    """Email the owner about a new critical finding, at most once a day per finding."""
    now = _now(now)
    owner = config.env("OWNER_EMAIL")
    if not (owner and config.env("RESEND_API_KEY") and config.env("SENDER_FROM_EMAIL")):
        return 0
    sent = 0
    for f in health(con, now):
        if f["severity"] != CRITICAL:
            continue
        key = f"alerted:{f['key']}"
        last = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if last and _utc(last[0]) > now - timedelta(hours=24):
            continue
        payload = {"from": f"MindLab Assistant <{config.env('SENDER_FROM_EMAIL')}>", "to": [owner], "subject": f"MindLab alert: {f['title']}",
                   "text": f"{f['title']}\n{f['detail']}\n\nWhat to do: {f['fix']}\n"}
        try:
            (post or (lambda p, k: emailing.resend_post(p, config.env("RESEND_API_KEY"), k)))(payload, f"alert-{f['key']}-{now.date()}")
        except Exception as e:
            log(f"alert failed: {e!r}")
            continue
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, now.isoformat(timespec="seconds")))
        con.commit()
        sent += 1
    return sent


# ---------------- suggested actions ----------------
def _id(args, key="id"):
    v = args.get(key)
    if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
        raise ValueError(f"{key} must be a positive whole number")
    return v


def _client_action(name):
    def run(con, args):
        followups.apply_action(con, _id(args), name)
        return f"client #{args['id']}: {name}"
    return run


def _popload(name):
    return lambda con, args: popload.apply_action(con, _id(args), name)


def _preview(name):
    return lambda con, args: previewui.apply_action(con, _id(args), name)


# The whole list of things the assistant may suggest. There is deliberately no action that starts outreach:
# no lead approval, no showcase approval, no prospect approval, no demo publishing (tests enforce this).
ACTIONS = {
    "prospect_reject": ("Reject a prospect so it is never emailed", _popload("reject")),
    "prospect_mark_replied": ("Mark a prospect as replied and cancel the rest of its sequence", _popload("replied")),
    "prospect_do_not_contact": ("Stop all contact with a prospect for good", _popload("dnc")),
    "lead_mark_replied": ("Mark a store-build lead as replied (no showcase email will be sent)", _preview("replied")),
    "lead_do_not_contact": ("Stop all contact with a lead for good and take its demo site down", _preview("dnc")),
    "preview_skip": ("Skip the day-7 showcase email for a lead", _preview("skip")),
    "demo_unpublish": ("Take a lead's demo site down", _preview("demo_unpublish")),
    "demo_extend": ("Extend a live demo site by 30 days", _preview("demo_extend")),
    "inbox_skip_draft": ("Skip a waiting reply draft (nothing is sent)", lambda con, args: inbox.skip_pending(con, _id(args))),
    "client_pause": ("Pause a client's follow-up emails", _client_action("pause")),
    "client_resume": ("Resume a paused client's follow-up emails", _client_action("resume")),
    "client_skip_next": ("Skip a client's next follow-up email", _client_action("skip_next")),
    "client_mark_done": ("Mark a client done and cancel their remaining follow-ups", _client_action("done")),
}
# Tasks that only tidy internal data (no email, no outside contact beyond reading public sites and your search API).
SAFE_TASKS = ("verify_prospects", "adopt_shopify_leads", "prepare_previews", "health_check")


def propose(con, action, args, reason="", source="chat"):
    """Queue a suggestion. Returns (proposal_id, None) or (None, why)."""
    if action not in ACTIONS:
        return None, f"unknown action '{action}'. Allowed: {', '.join(sorted(ACTIONS))}"
    if not isinstance(args, dict):
        return None, "args must be an object"
    try:
        _id(args)
    except ValueError as e:
        return None, str(e)
    if con.execute("SELECT 1 FROM assistant_proposals WHERE status='pending' AND action=? AND args=?", (action, json.dumps(args, sort_keys=True))).fetchone():
        return None, "that suggestion is already waiting for confirmation"
    summary = f"{ACTIONS[action][0]} (#{args['id']})"
    cur = con.execute("INSERT INTO assistant_proposals (created_at,action,args,summary,reason,source) VALUES (?,?,?,?,?,?)",
                      (db.now(), action, json.dumps(args, sort_keys=True), summary, str(reason)[:400], source))
    con.commit()
    return cur.lastrowid, None


def decide(con, proposal_id, confirm):
    """The owner's click. Returns a message. Only a pending proposal can be decided, and only once."""
    p = con.execute("SELECT * FROM assistant_proposals WHERE id=?", (proposal_id,)).fetchone()
    if not p or p["status"] != "pending":
        return "That suggestion is no longer pending."
    if not confirm:
        con.execute("UPDATE assistant_proposals SET status='dismissed', decided_at=? WHERE id=?", (db.now(), proposal_id))
        con.commit()
        return "Dismissed."
    try:
        result = ACTIONS[p["action"]][1](con, json.loads(p["args"])) or "done"
        status = "done"
    except Exception as e:
        result, status = f"failed: {e}", "failed"
    con.execute("UPDATE assistant_proposals SET status=?, result=?, decided_at=? WHERE id=?", (status, str(result)[:400], db.now(), proposal_id))
    con.commit()
    return f"{p['summary']}: {result}"


def run_safe_task(con, task, log=lambda *_: None):
    if task not in SAFE_TASKS:
        return f"unknown task. Allowed: {', '.join(SAFE_TASKS)}"
    if task == "verify_prospects":
        return f"checked {popload.verify_all(con, log=log)} prospect(s)"
    if task == "adopt_shopify_leads":
        return f"adopted {popload.adopt_shopify_leads(con, log=log)} store(s)"
    if task == "prepare_previews":
        return f"built {showcase.prepare(con, log=log)} preview(s)"
    return json.dumps(health(con))
