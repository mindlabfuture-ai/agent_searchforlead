import json, os, re, sqlite3, unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest import mock

from leadagent import assistant, assistantchat, assistantui, db, demosite, emailing, popload, previews

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app", "ADMIN_PASSWORD": "correct horse battery"}
NOW = datetime(2026, 10, 14, 2, 0, tzinfo=timezone.utc)  # Wed 10:00 PHT
CLEAN = {k: "" for k in ("EMAIL_SENDING_ENABLED", "RESEND_API_KEY", "SENDER_FROM_EMAIL", "RESEND_WEBHOOK_SECRET", "OWNER_EMAIL",
                         "SERPER_API_KEY", "BRAVE_API_KEY", "NETLIFY_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ASSISTANT_MODEL")}


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def add_emails(con, n, status="sent", days_ago=3):
    t = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    for i in range(n):
        con.execute("INSERT INTO emails (lead_id,to_email,subject,status,sent_at,kind) VALUES (?,?,?,?,?,'initial')", (i, f"a{i}@x.ph", "s", status, t))
    con.commit()


def keys(findings):
    return {f["key"]: f["severity"] for f in findings}


@mock.patch.dict(os.environ, {**CLEAN, **ENV})
class HealthTests(unittest.TestCase):
    def test_clean_dry_run_has_only_info(self):
        k = keys(assistant.health(mem(), NOW))
        self.assertEqual(set(k.values()), {"info"}); self.assertIn("dry_run", k)

    def test_missing_secrets_are_critical(self):
        with mock.patch.dict(os.environ, {"UNSUB_SECRET": "short", "BASE_URL": ""}):
            k = keys(assistant.health(mem(), NOW))
        self.assertEqual((k["unsub_secret"], k["base_url"]), ("critical", "critical"))

    def test_sending_on_requires_resend_settings(self):
        with mock.patch.dict(os.environ, {"EMAIL_SENDING_ENABLED": "true"}):
            k = keys(assistant.health(mem(), NOW))
        self.assertEqual({k["resend_api_key"], k["sender_from_email"], k["resend_webhook_secret"]}, {"critical"})

    def test_deliverability_thresholds(self):
        con = mem(); add_emails(con, 90); add_emails(con, 10, "bounced")           # 10% bounced
        self.assertEqual(keys(assistant.health(con, NOW))["deliverability"], "critical")
        con = mem(); add_emails(con, 95); add_emails(con, 5, "bounced")            # 5%
        self.assertEqual(keys(assistant.health(con, NOW))["deliverability"], "warn")
        con = mem(); add_emails(con, 98); add_emails(con, 2, "bounced")            # 2%
        self.assertNotIn("deliverability", keys(assistant.health(con, NOW)))
        con = mem(); add_emails(con, 5); add_emails(con, 5, "bounced")             # too few to judge
        self.assertNotIn("deliverability", keys(assistant.health(con, NOW)))
        con = mem(); add_emails(con, 99); add_emails(con, 1, "complained")         # 1% spam complaints
        self.assertEqual(keys(assistant.health(con, NOW))["deliverability"], "critical")

    def test_failed_sends_and_silent_webhook(self):
        con = mem(); add_emails(con, 2, "failed", days_ago=0)
        self.assertEqual(keys(assistant.health(con, NOW))["failed_sends"], "warn")
        con = mem(); add_emails(con, 6)
        with mock.patch.dict(os.environ, {"EMAIL_SENDING_ENABLED": "true", "RESEND_API_KEY": "k", "SENDER_FROM_EMAIL": "a@b.ph", "RESEND_WEBHOOK_SECRET": "s"}):
            self.assertEqual(keys(assistant.health(con, NOW))["webhook_silent"], "warn")
        add_emails(con, 1, "delivered")
        with mock.patch.dict(os.environ, {"EMAIL_SENDING_ENABLED": "true", "RESEND_API_KEY": "k", "SENDER_FROM_EMAIL": "a@b.ph", "RESEND_WEBHOOK_SECRET": "s"}):
            self.assertNotIn("webhook_silent", keys(assistant.health(con, NOW)))

    def test_demo_sites(self):
        con = mem()
        con.execute("INSERT INTO demo_sites (lead_id,slug,status,expires_at) VALUES (1,'a','live',?)", ((NOW + timedelta(days=2)).isoformat(),)); con.commit()
        k = keys(assistant.health(con, NOW))
        self.assertEqual((k["demo_no_token"], k["demo_expiring"]), ("critical", "info"))

    def test_worst_first(self):
        with mock.patch.dict(os.environ, {"BASE_URL": ""}):
            sev = [f["severity"] for f in assistant.health(mem(), NOW)]
        self.assertEqual(sev, sorted(sev, key=assistant.RANK.get))


@mock.patch.dict(os.environ, {**CLEAN, **ENV})
class BriefingTests(unittest.TestCase):
    def test_attention_counts_and_links(self):
        con = mem()
        for i, st in enumerate(("qualified", "drafted", "approved")):
            db.upsert_lead(con, f"https://facebook.com/p{i}", f"P{i}", "x"); con.execute("UPDATE leads SET status=? WHERE url LIKE ?", (st, f"%/p{i}"))
        popload.add_rows(con, [{"name": "A", "website": "a.ph"}]); con.execute("UPDATE prospects SET status='verified'"); con.commit()
        got = {label.split(" ")[0]: (n, link) for label, n, link in assistant.attention(con, NOW)}
        self.assertEqual(got["first"], (2, "/")); self.assertEqual(got["verified"], (1, "/prospects?show=verified"))
        self.assertEqual(assistant.attention(mem(), NOW), [])

    def test_showcase_and_sequence_reminders(self):
        con = mem()
        db.upsert_lead(con, "https://facebook.com/g", "G", "x"); con.execute("UPDATE leads SET status='contacted', email='o@g.ph'")
        con.execute("INSERT INTO emails (lead_id,to_email,subject,status,sent_at,kind) VALUES (1,'o@g.ph','s','sent',?,'initial')", ((NOW - timedelta(days=5)).isoformat(),))
        previews.generate(con, con.execute("SELECT * FROM leads").fetchone(), lambda u: None, lambda u: None)
        self.assertTrue(any("showcase" in l for l, n, _ in assistant.attention(con, NOW)))
        popload.add_rows(con, [{"name": "A", "website": "a.ph"}])
        con.execute("UPDATE prospects SET status='verified', email='a@a.ph'"); con.commit()
        popload.apply_action(con, 1, "approve")
        con.execute("UPDATE prospect_steps SET status='sent', sent_at=? WHERE step='intro'", ((NOW - timedelta(days=3, hours=20)).isoformat(),)); con.commit()
        self.assertTrue(any("inbox for replies" in l for l, n, _ in assistant.attention(con, NOW)))

    def test_briefing_text(self):
        con = mem(); b = assistant.briefing(con, NOW)
        text = assistant.render_text(b, "https://leads.example.app")
        self.assertIn("daily brief", text); self.assertIn("Sending is OFF", text)
        self.assertEqual(b["day"], "2026-10-14")

    def test_brief_is_stored_and_emailed_once(self):
        con = mem(); sent = []
        env = {"OWNER_EMAIL": "me@mindlab.ph", "RESEND_API_KEY": "k", "SENDER_FROM_EMAIL": "hello@mail.ph"}
        with mock.patch.dict(os.environ, env):
            assistant.store_and_send(con, NOW, post=lambda p, k: sent.append((p, k)), log=lambda *_: None)
            assistant.store_and_send(con, NOW, post=lambda p, k: sent.append((p, k)), log=lambda *_: None)
        self.assertEqual(len(sent), 1); self.assertEqual(sent[0][0]["to"], ["me@mindlab.ph"]); self.assertEqual(sent[0][1], "brief-2026-10-14")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM assistant_briefings").fetchone()[0], 1)

    def test_no_owner_email_no_send_and_failure_is_contained(self):
        con = mem(); post = mock.Mock()
        assistant.store_and_send(con, NOW, post=post, log=lambda *_: None); post.assert_not_called()
        env = {"OWNER_EMAIL": "me@mindlab.ph", "RESEND_API_KEY": "k", "SENDER_FROM_EMAIL": "hello@mail.ph"}
        with mock.patch.dict(os.environ, env):
            assistant.store_and_send(con, NOW + timedelta(days=1), post=mock.Mock(side_effect=OSError("down")), log=lambda *_: None)

    def test_critical_alert_once_a_day(self):
        con = mem(); sent = []
        env = {"OWNER_EMAIL": "me@mindlab.ph", "RESEND_API_KEY": "k", "SENDER_FROM_EMAIL": "hello@mail.ph", "BASE_URL": ""}
        with mock.patch.dict(os.environ, env):
            post = lambda p, k: sent.append(p)
            self.assertEqual(assistant.alert_critical(con, NOW, post=post), 1)
            self.assertEqual(assistant.alert_critical(con, NOW + timedelta(hours=2), post=post), 0)
            self.assertEqual(assistant.alert_critical(con, NOW + timedelta(hours=25), post=post), 1)
        self.assertIn("BASE_URL", sent[0]["subject"])


@mock.patch.dict(os.environ, {**CLEAN, **ENV})
class ActionTests(unittest.TestCase):
    def prospect(self, con):
        popload.add_rows(con, [{"name": "A", "website": "a.ph"}])
        con.execute("UPDATE prospects SET status='verified', email='a@a.ph'"); con.commit()

    def test_assistant_cannot_start_outreach(self):
        banned = {"approve", "publish", "send", "start", "launch", "enable"}
        self.assertFalse([a for a in assistant.ACTIONS if banned & set(a.split("_"))])
        self.assertFalse([t for t in assistant.SAFE_TASKS if banned & set(t.split("_"))])
        names = {t["name"] for t in assistantchat.TOOLS}
        self.assertEqual(names, {"get_overview", "list_items", "get_item", "propose_action", "run_task"})

    def test_propose_validates(self):
        con = mem()
        self.assertIn("unknown action", assistant.propose(con, "prospect_approve", {"id": 1})[1])
        self.assertIn("positive whole number", assistant.propose(con, "prospect_reject", {"id": "1; DROP"})[1])
        self.assertIn("positive whole number", assistant.propose(con, "prospect_reject", {"id": True})[1])
        pid, why = assistant.propose(con, "prospect_reject", {"id": 1}, "dead domain"); self.assertIsNone(why)
        self.assertIn("already waiting", assistant.propose(con, "prospect_reject", {"id": 1})[1])

    def test_nothing_runs_until_confirmed(self):
        con = mem(); self.prospect(con)
        pid, _ = assistant.propose(con, "prospect_reject", {"id": 1}, "x")
        self.assertEqual(con.execute("SELECT status FROM prospects").fetchone()[0], "verified")
        self.assertIn("Rejected", assistant.decide(con, pid, True))
        self.assertEqual(con.execute("SELECT status FROM prospects").fetchone()[0], "rejected")
        self.assertIn("no longer pending", assistant.decide(con, pid, True))     # a proposal runs once

    def test_dismiss_changes_nothing(self):
        con = mem(); self.prospect(con)
        pid, _ = assistant.propose(con, "prospect_reject", {"id": 1})
        assistant.decide(con, pid, False)
        self.assertEqual(con.execute("SELECT status FROM prospects").fetchone()[0], "verified")
        self.assertEqual(con.execute("SELECT status FROM assistant_proposals").fetchone()[0], "dismissed")

    def test_failed_action_is_recorded_not_raised(self):
        con = mem(); pid, _ = assistant.propose(con, "client_pause", {"id": 99}); 
        with mock.patch.dict(assistant.ACTIONS, {"client_pause": ("x", mock.Mock(side_effect=RuntimeError("boom")))}):
            self.assertIn("failed", assistant.decide(con, pid, True))
        self.assertEqual(con.execute("SELECT status FROM assistant_proposals").fetchone()[0], "failed")

    def test_do_not_contact_suppresses_and_cancels(self):
        con = mem(); self.prospect(con); popload.apply_action(con, 1, "approve")
        pid, _ = assistant.propose(con, "prospect_do_not_contact", {"id": 1}); assistant.decide(con, pid, True)
        self.assertTrue(emailing.is_suppressed(con, "a@a.ph"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM prospect_steps WHERE status='pending'").fetchone()[0], 0)

    def test_safe_tasks(self):
        con = mem()
        self.assertIn("unknown task", assistant.run_safe_task(con, "send_everything"))
        self.assertIn("checked 0", assistant.run_safe_task(con, "verify_prospects"))
        json.loads(assistant.run_safe_task(con, "health_check"))


def block(kind, **kw):
    return NS(type=kind, **kw)


def reply(content, stop):
    return NS(content=content, stop_reason=stop)


class FakeClient:
    """Plays back scripted responses and records each request."""
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(json.loads(json.dumps(kw, default=lambda o: f"<{type(o).__name__}>")))
        self.last_messages = kw["messages"]
        return self.responses.pop(0)


@mock.patch.dict(os.environ, {**CLEAN, **ENV})
class ChatTests(unittest.TestCase):
    def test_needs_a_key(self):
        text, note = assistantchat.ask(mem(), "hi")
        self.assertEqual(text, ""); self.assertIn("ANTHROPIC_API_KEY", note)

    def test_tool_loop_and_history(self):
        con = mem()
        fc = FakeClient(reply([block("thinking", thinking=""), block("tool_use", id="t1", name="get_overview", input={}),
                               block("tool_use", id="t2", name="list_items", input={"kind": "prospects"})], "tool_use"),
                        reply([block("text", text="All quiet.")], "end_turn"))
        text, note = assistantchat.ask(con, "What should I do today?", client=fc)
        self.assertEqual((text, note), ("All quiet.", ""))
        results = fc.last_messages[-2]
        self.assertEqual(results["role"], "user"); self.assertEqual([r["tool_use_id"] for r in results["content"]], ["t1", "t2"])  # both in one message
        self.assertEqual((fc.last_messages[-3]["role"], fc.last_messages[-1]["role"]), ("assistant", "assistant"))
        req = fc.requests[0]
        self.assertEqual(req["model"], "claude-opus-5-5"); self.assertEqual(req["fallbacks"], "default")
        self.assertNotIn("tool_choice", req); self.assertNotIn("thinking", req)
        self.assertEqual([m["role"] for m in assistantchat.history(con)], ["user", "assistant"])
        fc2 = FakeClient(reply([block("text", text="ok")], "end_turn"))
        assistantchat.ask(con, "and now?", client=fc2)
        self.assertEqual([m["role"] for m in fc2.last_messages], ["user", "assistant", "user", "assistant"])

    def test_refusal_and_runaway_loops_and_limit(self):
        con = mem()
        self.assertIn("can't help", assistantchat.ask(con, "x", client=FakeClient(reply([], "refusal")))[0])
        loop = [reply([block("tool_use", id=f"t{i}", name="get_overview", input={})], "tool_use") for i in range(assistantchat.MAX_TURNS)]
        self.assertIn("too many steps", assistantchat.ask(mem(), "x", client=FakeClient(*loop))[0])
        with mock.patch.dict(os.environ, {"ASSISTANT_DAILY_LIMIT": "1"}):
            con = mem(); assistantchat.ask(con, "one", client=FakeClient(reply([block("text", text="a")], "end_turn")))
            self.assertIn("limit", assistantchat.ask(con, "two", client=FakeClient())[1])

    def test_model_can_only_suggest(self):
        con = mem(); popload.add_rows(con, [{"name": "A", "website": "a.ph"}]); con.execute("UPDATE prospects SET status='verified', email='a@a.ph'"); con.commit()
        r = assistantchat.run_tool(con, "propose_action", {"action": "prospect_approve", "id": 1, "reason": "looks great"})
        self.assertIn("error", r)
        r = assistantchat.run_tool(con, "propose_action", {"action": "prospect_reject", "id": 1, "reason": "x"})
        self.assertIn("proposal_id", r); self.assertEqual(con.execute("SELECT status FROM prospects").fetchone()[0], "verified")

    def test_tools_are_safe_and_bounded(self):
        con = mem()
        self.assertIn("error", assistantchat.run_tool(con, "nope", {}))
        self.assertIn("error", assistantchat.run_tool(con, "get_item", {"kind": "lead", "id": 5}))
        for i in range(40):
            db.upsert_lead(con, f"https://facebook.com/p{i}", "N" * 300, "x" * 300)
        self.assertLessEqual(len(assistantchat.run_tool(con, "list_items", {"kind": "leads", "limit": 99})), 30)
        self.assertLessEqual(len(assistantchat._clip(assistantchat.run_tool(con, "list_items", {"kind": "leads", "limit": 30}))), assistantchat.MAX_TOOL_CHARS + 60)
        self.assertNotIn("draft", assistantchat.run_tool(con, "get_item", {"kind": "lead", "id": 1}))
        json.dumps(assistantchat.run_tool(con, "list_items", {"kind": "failed_emails"}))
        json.dumps(assistantchat.run_tool(con, "list_items", {"kind": "demos"}))


@mock.patch.dict(os.environ, {**CLEAN, **ENV})
class PageTests(unittest.TestCase):
    def test_page_escapes_and_shows_proposals(self):
        con = mem(); assistant.propose(con, "prospect_reject", {"id": 1}, "<script>x</script>")
        page = assistantui.render(con, "tok")
        self.assertNotIn("<script>x", page); self.assertIn("Confirm", page); self.assertIn("ANTHROPIC_API_KEY", page)
        self.assertEqual(assistantui.critical_count(con), 0)


if __name__ == "__main__":
    unittest.main()
