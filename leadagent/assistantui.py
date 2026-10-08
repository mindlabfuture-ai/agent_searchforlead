"""The Assistant page: today's brief, suggestions waiting for your click, and a chat box."""
import html

from . import assistant, assistantchat, config

E = lambda v: html.escape(str(v if v is not None else ""), quote=True)
STYLE = ("<style>body{font-family:system-ui;max-width:900px;margin:1rem auto;padding:0 16px}section{border:1px solid #ccc;border-radius:8px;padding:12px;margin:12px 0}"
         ".b{padding:8px;border-radius:6px}button{margin:2px;padding:6px 12px}.crit{background:#fde;border-color:#d99}.warn{background:#ffd;border-color:#dd9}"
         ".me{background:#eef;border-radius:8px;padding:8px;margin:6px 0}.bot{background:#f6f6f6;border-radius:8px;padding:8px;margin:6px 0;white-space:pre-wrap}"
         "textarea{width:100%;box-sizing:border-box;padding:6px}nav a{margin-right:12px}small{color:#555}</style>")


def critical_count(con):
    return sum(f["severity"] == assistant.CRITICAL for f in assistant.health(con))


def render(con, tok, message="", answer="", question=""):
    tok = f"<input type=hidden name=csrf value={tok}>"
    b = assistant.briefing(con)
    health = "".join(f"<div class='b {'crit' if f['severity'] == 'critical' else 'warn' if f['severity'] == 'warn' else ''}' style='margin:6px 0'>"
                     f"<b>{E(f['title'])}</b>{('<br>' + E(f['detail'])) if f['detail'] else ''}{('<br><small>Fix: ' + E(f['fix']) + '</small>') if f['fix'] else ''}</div>"
                     for f in b["health"]) or "<p>No problems found.</p>"
    attn = "".join(f"<li><b>{n}</b> {E(label)} &middot; <a href='{E(link)}'>open</a></li>" for label, n, link in b["attention"]) or "<li>Nothing needs you right now.</li>"
    props = ""
    for p in assistant.pending_proposals(con):
        props += (f"<div class=b style='background:#eef;margin:6px 0'><b>{E(p['summary'])}</b>{('<br>' + E(p['reason'])) if p['reason'] else ''}"
                  f"<form method=post action=/assistant style=display:inline>{tok}<input type=hidden name=mode value=decide><input type=hidden name=id value={p['id']}>"
                  f"<button name=choice value=confirm>Confirm</button><button name=choice value=dismiss>Dismiss</button></form></div>")
    recent = con.execute("SELECT summary, status, result FROM assistant_proposals WHERE status!='pending' ORDER BY id DESC LIMIT 5").fetchall()
    done = "".join(f"<li>{E(r['summary'])}: {E(r['status'])}{(' - ' + E(r['result'])) if r['result'] else ''}</li>" for r in recent)
    s = b["stats"]
    stats = (f"Emails today {s['emails_today']}/{s['email_cap']} &middot; last 30 days {s['emails_30d']} &middot; suppressed {s['suppressed']} &middot; "
             f"active clients {s['clients_active']} &middot; live demos {s['demos_live']} &middot; sending is <b>{'ON' if s['sending_on'] else 'OFF (dry run)'}</b>")
    if assistantchat.enabled():
        chat = "".join(f"<div class={'me' if m['role'] == 'user' else 'bot'}>{E(m['content'])}</div>" for m in assistantchat.history(con, 10))
        if answer:
            chat += f"<div class=me>{E(question)}</div><div class=bot>{E(answer)}</div>" if not chat.endswith(E(answer) + "</div>") else ""
        chat_html = (f"<section><h3>Ask the assistant</h3>{chat}<form method=post action=/assistant>{tok}<input type=hidden name=mode value=ask>"
                     f"<textarea name=question rows=3 maxlength=2000 placeholder='What should I do today? Which prospects look strongest? Why did sending stop?'></textarea>"
                     f"<p><button>Ask</button> <small>It can look things up and suggest actions. It cannot send or approve anything.</small></p></form></section>")
    else:
        chat_html = "<section><h3>Ask the assistant</h3><p>Set <code>ANTHROPIC_API_KEY</code> in Railway to turn on the chat. The brief and health checks work without it.</p></section>"
    note = f"<p class=b style='background:#eef'>{E(message)}</p>" if message else ""
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Assistant</title>{STYLE}"
            f"<nav><a href='/'>Lead queue</a><a href='/previews'>Previews</a><a href='/prospects'>POPLoad prospects</a><a href='/clients'>Clients</a></nav>"
            f"<h1>Assistant</h1><p><b>{E(b['headline'])}</b></p>{note}<section><h3>Health</h3>{health}</section>"
            f"<section><h3>Needs you</h3><ul>{attn}</ul><p><small>{stats}</small></p></section>"
            f"<section><h3>Suggestions waiting for you</h3>{props or '<p>None.</p>'}"
            f"{('<p><small>Recently decided:</small></p><ul>' + done + '</ul>') if done else ''}</section>{chat_html}"
            f"<p><small>The assistant never starts outreach: you approve every first email, showcase email, prospect sequence and demo site yourself. "
            f"A daily brief is emailed to OWNER_EMAIL ({'set' if config.env('OWNER_EMAIL') else 'not set'}).</small></p>")
