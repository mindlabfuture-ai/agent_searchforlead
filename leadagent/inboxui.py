"""The Inbox page: drafts waiting for your Send, and what the inbox agent has read lately."""
import html

from . import config, inbox

E = lambda v: html.escape(str(v if v is not None else ""), quote=True)
STYLE = ("<style>body{font-family:system-ui;max-width:900px;margin:1rem auto;padding:0 16px}section{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}"
         ".b{padding:8px;border-radius:6px}button{margin:2px;padding:6px 12px}textarea{width:100%;box-sizing:border-box;padding:6px}small{color:#555}nav a{margin-right:12px}"
         "table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eee;padding:4px 6px;text-align:left;font-size:14px}.no{color:#a00}</style>")


def render(con, tok, message=""):
    tok = f"<input type=hidden name=csrf value={tok}>"
    pend = ""
    for p in con.execute("SELECT * FROM inbox_pending WHERE status='waiting' ORDER BY id").fetchall():
        pend += (f"<section><b>{'Follow-up' if p['kind'] == 'nurture' else 'Reply'} to {E(p['email'])}</b> <small>{E(p['subject'])}</small>"
                 f"<form method=post action=/inbox>{tok}<input type=hidden name=id value={p['id']}>"
                 f"<textarea name=body rows=7>{E(p['body'])}</textarea><p><button name=mode value=send>Send</button>"
                 f"<button name=mode value=skip>Skip</button> <small>You can edit the text before sending.</small></p></form></section>")
    rows = "".join(f"<tr><td>{E(r['ts'][:16])}</td><td>{E(r['addr'])}</td><td>{E(r['subject'])}</td><td>{E(r['category'] or '')}</td>"
                   f"<td>{E(r['priority'] or '')}</td><td>{E(r['status'])}{(' <small>' + E(r['matched']) + '</small>') if r['matched'] else ''}</td>"
                   f"<td>{E(r['summary'] or r['note'] or '')}</td></tr>"
                   for r in con.execute("SELECT * FROM inbox_messages ORDER BY id DESC LIMIT 40").fetchall())
    last_ok = con.execute("SELECT value FROM meta WHERE key='inbox_last_ok'").fetchone()
    last_err = con.execute("SELECT value FROM meta WHERE key='inbox_last_error'").fetchone()
    state = (f"Reading {E(inbox.mail_user())}; last successful read {E(last_ok[0][:16]) if last_ok else 'never'}; reply mode <b>{inbox.reply_mode()}</b>."
             if inbox.enabled() else "The inbox agent is <b>off</b>. Set <code>INBOX_ENABLED=true</code> and <code>MAIL_PASSWORD</code> in Railway (after stopping the old standalone agent).")
    err = f"<p class=no>Last problem: {E(last_err[0])}</p>" if last_err else ""
    note = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Inbox</title>{STYLE}"
            f"<nav><a href='/'>Lead queue</a><a href='/assistant'>Assistant</a><a href='/prospects'>POPLoad prospects</a><a href='/clients'>Clients</a></nav>"
            f"<h1>Inbox</h1><p>{state}</p>{err}{note}<h2>Drafts waiting for you</h2>{pend or '<p>None.</p>'}"
            f"<h2>Recently read</h2><table><tr><th>When (UTC)</th><th>From</th><th>Subject</th><th>Category</th><th>Priority</th><th>What happened</th><th>Summary</th></tr>{rows}</table>"
            f"<p><small>Messages that answer one of our outreach emails stop that lead's sequence automatically; a reply to them is always a draft for you. "
            f"Subjects and summaries are kept, never message bodies.</small></p>")
