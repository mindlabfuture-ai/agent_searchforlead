import os, sqlite3, unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from leadagent import config, db, emailing, followups

ENV = {"UNSUB_SECRET": "x" * 32, "BASE_URL": "https://leads.example.app", "SENDER_FROM_EMAIL": "hello@mail.example.ph",
       "RESEND_API_KEY": "re_test", "FOLLOWUP_DAILY_CAP": "2"}
NOW = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)          # Wed 10:00 Philippine time
TODAY = date(2026, 10, 7)
SAT = datetime(2026, 10, 10, 2, 0, tzinfo=timezone.utc)


def mem():
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA); return con


def client(con, name="Glow PH", email="owner@glow.ph", handed="2026-10-07", **kw):
    cid, err = followups.add_client(con, name, email, kw.pop("store_url", "https://glowph.myshopify.com"),
                                    handed_over=handed, today=TODAY, **kw)
    assert err is None, err
    return cid


def steps(con, cid):
    return {r["step"]: (r["due_at"], r["status"]) for r in con.execute("SELECT * FROM followups WHERE client_id=?", (cid,))}


def plain(html):
    import html as H, re
    return H.unescape(re.sub(r"<[^>]+>", "", html))


def send_all(con, now=NOW, **kw):
    sent = []
    n = followups.run(con, post=lambda p, k: sent.append((p, k)) or {"id": f"re_{len(sent)}"}, now=now, enabled=True,
                      log=lambda *_: None, **kw)
    return n, sent


@mock.patch.dict(os.environ, ENV)
class ScheduleTests(unittest.TestCase):
    def test_schedule_and_won_lead(self):
        con = mem(); db.upsert_lead(con, "https://facebook.com/glowph", "Glow PH")
        lid = con.execute("SELECT id FROM leads").fetchone()[0]
        cid, err = followups.add_client(con, "Glow PH", "Owner@Glow.ph", "https://glowph.myshopify.com", handed_over="2026-10-07", lead_id=lid, today=TODAY)
        self.assertIsNone(err)
        self.assertEqual(steps(con, cid), {"welcome": ("2026-10-07", "pending"), "popload_check": ("2026-10-14", "pending"),
                                           "growth": ("2026-11-06", "pending"), "next_level": ("2026-12-06", "pending")})
        self.assertEqual(con.execute("SELECT email FROM clients").fetchone()[0], "owner@glow.ph")
        self.assertEqual(con.execute("SELECT status FROM leads").fetchone()[0], "won")

    def test_backdated_handover_skips_steps_that_are_already_too_late(self):
        con = mem(); cid = client(con, handed="2026-08-01")
        self.assertEqual({k: v[1] for k, v in steps(con, cid).items()},
                         {"welcome": "skipped", "popload_check": "skipped", "growth": "skipped", "next_level": "pending"})

    def test_validation(self):
        con = mem(); ok = lambda **kw: followups.add_client(con, kw.get("name", "A"), kw.get("email", "a@b.ph"), kw.get("store", ""), today=TODAY, **kw.get("rest", {}))
        self.assertIn("name", ok(name="")[1]); self.assertIn("email", ok(email="nope")[1])
        self.assertIn("email", ok(email="noreply@brand.com")[1])
        self.assertIn("https://", ok(store="http://shop.ph")[1]); self.assertIn("https://", ok(store="javascript:alert(1)")[1])
        self.assertIn("language", ok(rest={"lang": "fr"})[1]); self.assertIn("POPLoad", ok(rest={"popload_status": "x"})[1])
        self.assertIn("date", ok(rest={"handed_over": "yesterday"})[1])
        self.assertIsNone(ok()[1]); self.assertIn("already", ok()[1])
        emailing.suppress(con, "gone@b.ph", "unsubscribed"); self.assertIn("opted out", ok(email="gone@b.ph")[1])


@mock.patch.dict(os.environ, ENV)
class MessageTests(unittest.TestCase):
    def test_every_step_and_language_is_branded_compliant_and_clean(self):
        con = mem()
        for lang, name in (("en", "Glow PH"), ("tl", "Kapeng Bukid")):
            cid = client(con, name=name, email=f"{lang}@shop.ph", lang=lang, popload_status="installed")
            c = con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
            for step, _ in followups.STEPS:
                m = followups.build_followup(c, step, config.base_url()); h = m["html"]
                for brand in ("#060A12", "#E3B965", "Space Grotesk", "mindlabfuture-ai.com/img/logo-ml.png"):
                    self.assertIn(brand, h)
                self.assertIn("/unsubscribe?t=", h); self.assertIn("/unsubscribe?t=", m["text"])
                self.assertEqual(m["headers"]["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
                self.assertEqual(h.count("<img"), 1); self.assertNotIn("<script", h)
                self.assertIn("built your Shopify store", m["text"]); self.assertNotIn("publicly listed", m["text"])
                self.assertIn("1. ", m["text"]); self.assertIn("Taguig", m["text"])
                self.assertTrue(m["subject"] in plain(h) and m["subject"])
                for part in (m["text"], plain(h)):
                    for banned in ("$1/", "guarantee", "refund", "free trial", "₱1,000"):
                        self.assertNotIn(banned, part.lower())

    def test_welcome_depends_on_popload_status(self):
        con = mem()
        a = con.execute("SELECT * FROM clients WHERE id=?", (client(con, email="a@shop.ph", popload_status="not_installed"),)).fetchone()
        b = con.execute("SELECT * FROM clients WHERE id=?", (client(con, email="b@shop.ph", popload_status="installed"),)).fetchone()
        self.assertIn("Reply POPLOAD", followups.build_followup(a, "welcome", config.base_url())["text"])
        t = followups.build_followup(b, "welcome", config.base_url())["text"]
        self.assertIn("Add your payment details", t); self.assertNotIn("Reply POPLOAD", t)

    def test_hostile_name_and_store_are_escaped_or_flattened(self):
        con = mem(); cid = client(con, name='Glow <img src=x onerror=alert(1)>\n\nPH', store_url="https://a.ph/?x=<b>")
        c = con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        m = followups.build_followup(c, "welcome", config.base_url())
        self.assertNotIn("<img src=x", m["html"]); self.assertNotIn("<b>", m["html"]); self.assertEqual(m["html"].count("<img"), 1)

    def test_no_store_link_reads_cleanly(self):
        con = mem(); cid = client(con, store_url="")
        t = followups.build_followup(con.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone(), "welcome", config.base_url())["text"]
        self.assertIn("is now yours. Here is how", t)


@mock.patch.dict(os.environ, ENV)
class SendingTests(unittest.TestCase):
    def test_dry_run_changes_nothing(self):
        con = mem(); cid = client(con); logs = []; sent = []
        n = followups.run(con, post=lambda p, k: sent.append(p), now=NOW, enabled=False, log=logs.append)
        self.assertEqual((n, sent), (0, [])); self.assertTrue(any("DRY RUN" in l for l in logs))
        self.assertEqual(steps(con, cid)["welcome"][1], "pending")

    def test_live_send_marks_sent_uses_idempotency_key_and_never_repeats(self):
        con = mem(); cid = client(con)
        n, sent = send_all(con)
        self.assertEqual(n, 1); self.assertEqual(sent[0][1], f"client-{cid}-welcome"); self.assertEqual(sent[0][0]["to"], ["owner@glow.ph"])
        self.assertEqual(sent[0][0]["reply_to"], "support@mindlabfuture-ai.com"); self.assertIn("List-Unsubscribe", sent[0][0]["headers"])
        self.assertEqual(steps(con, cid)["welcome"][1], "sent")
        self.assertEqual(send_all(con)[0], 0)

    def test_business_hours_only(self):
        con = mem(); client(con)
        self.assertEqual(send_all(con, now=SAT)[0], 0)

    def test_minimum_gap_and_daily_cap(self):
        con = mem(); cid = client(con, handed="2026-10-05")                # welcome 2 days ago, popload_check due on the 12th
        n, _ = send_all(con); self.assertEqual(n, 1)                      # welcome goes out (2 days late, still allowed)
        con.execute("UPDATE followups SET due_at='2026-10-07' WHERE client_id=? AND step='popload_check'", (cid,))
        con.execute("UPDATE followups SET sent_at=? WHERE client_id=? AND step='welcome'", ((NOW - timedelta(days=2)).isoformat(timespec="seconds"), cid))
        self.assertEqual(send_all(con)[0], 0)                             # only 2 days since the last one
        con.execute("UPDATE followups SET sent_at=? WHERE client_id=? AND step='welcome'", ((NOW - timedelta(days=4)).isoformat(timespec="seconds"), cid))
        self.assertEqual(send_all(con)[0], 1)
        con2 = mem()
        for i in range(4): client(con2, email=f"o{i}@shop.ph", name=f"S{i}")
        self.assertEqual(send_all(con2)[0], 2)                            # FOLLOWUP_DAILY_CAP=2
        self.assertEqual(send_all(con2)[0], 0)

    def test_overdue_steps_are_skipped_not_sent_late(self):
        con = mem(); cid = client(con, handed="2026-10-07")
        con.execute("UPDATE followups SET due_at='2026-09-20' WHERE client_id=? AND step='welcome'", (cid,))
        n, _ = send_all(con); self.assertEqual(n, 0)
        self.assertEqual(steps(con, cid)["welcome"][1], "skipped")

    def test_popload_check_skipped_when_already_active(self):
        con = mem(); cid = client(con, popload_status="active")
        con.execute("UPDATE followups SET status='sent' WHERE step='welcome'")
        con.execute("UPDATE followups SET due_at='2026-10-07' WHERE step='popload_check'")
        self.assertEqual(send_all(con)[0], 0); self.assertEqual(steps(con, cid)["popload_check"][1], "skipped")

    def test_paused_and_done_clients_get_nothing(self):
        con = mem(); cid = client(con); followups.apply_action(con, cid, "pause", today=TODAY)
        self.assertEqual(send_all(con)[0], 0)
        followups.apply_action(con, cid, "resume", today=TODAY); self.assertEqual(send_all(con)[0], 1)

    def test_unsubscribed_or_bounced_address_ends_the_sequence(self):
        con = mem(); cid = client(con); emailing.suppress(con, "owner@glow.ph", "unsubscribed")
        n, sent = send_all(con); self.assertEqual((n, sent), (0, []))
        self.assertEqual(con.execute("SELECT status FROM clients").fetchone()[0], "done")
        self.assertTrue(all(v[1] == "skipped" for v in steps(con, cid).values()))

    def test_failures_retry_then_give_up(self):
        con = mem(); cid = client(con)
        def boom(p, k): raise OSError("network down")
        for _ in range(followups.MAX_ATTEMPTS):
            followups.run(con, post=boom, now=NOW, enabled=True, log=lambda *_: None)
        self.assertEqual(steps(con, cid)["welcome"][1], "failed")
        self.assertEqual(send_all(con)[0], 0)

    def test_needs_base_url_for_the_unsubscribe_link(self):
        con = mem(); client(con)
        with mock.patch.dict(os.environ, {"BASE_URL": "", "RAILWAY_PUBLIC_DOMAIN": ""}):
            self.assertEqual(send_all(con)[0], 0)

    def test_spacing_between_sends(self):
        con = mem(); [client(con, email=f"o{i}@shop.ph", name=f"S{i}") for i in range(2)]
        pauses = []; n, _ = send_all(con, sleep=pauses.append)
        self.assertEqual((n, len(pauses)), (2, 2)); self.assertTrue(all(20 <= p <= 60 for p in pauses))


@mock.patch.dict(os.environ, ENV)
class ActionTests(unittest.TestCase):
    def test_resume_respaces_steps_that_came_due_while_paused(self):
        con = mem(); cid = client(con, handed="2026-09-25")                # welcome 9/25, check 10/2 (late), growth 10/25
        followups.apply_action(con, cid, "pause", today=TODAY)
        con.execute("UPDATE followups SET status='sent' WHERE step='welcome'")
        followups.apply_action(con, cid, "resume", today=TODAY)
        self.assertEqual(steps(con, cid)["popload_check"], ("2026-10-08", "pending"))

    def test_done_popload_and_skip_next(self):
        con = mem(); cid = client(con)
        followups.apply_action(con, cid, "popload_active", today=TODAY)
        self.assertEqual(con.execute("SELECT popload_status FROM clients").fetchone()[0], "active")
        followups.apply_action(con, cid, "skip_next", today=TODAY); self.assertEqual(steps(con, cid)["welcome"][1], "skipped")
        followups.apply_action(con, cid, "bogus", today=TODAY); followups.apply_action(con, 999, "pause", today=TODAY)
        followups.apply_action(con, cid, "done", today=TODAY)
        self.assertEqual(con.execute("SELECT status FROM clients").fetchone()[0], "done")
        self.assertTrue(all(v[1] == "skipped" for v in steps(con, cid).values()))


if __name__ == "__main__":
    unittest.main()


@mock.patch.dict(os.environ, {**ENV, "ADMIN_PASSWORD": "correct horse battery"})
class ClientsPageTests(unittest.TestCase):
    def test_page_escapes_everything_and_has_csrf(self):
        from leadagent import server
        con = mem(); cid = client(con, name="<script>alert(1)</script>", store_url="https://a.ph/x?y=\"onmouseover=alert(1)")
        con.execute("UPDATE clients SET notes='<b>n</b>'")
        out = server.render_clients_page(con, "<img src=x onerror=alert(1)>")
        for raw in ("<script>alert(1)</script>", "<img src=x", 'href="https://a.ph/x?y="onmouseover'):
            self.assertNotIn(raw, out)
        self.assertIn("&lt;script&gt;", out); self.assertIn(server.csrf_token(), out); self.assertIn("DRY RUN", out)
        self.assertIn("welcome: due 2026-10-07", out)

    def test_page_shows_state_and_buttons(self):
        from leadagent import server
        con = mem(); cid = client(con)
        con.execute("UPDATE followups SET status='sent', sent_at='2026-10-07T02:00:00+00:00' WHERE step='welcome'")
        out = server.render_clients_page(con)
        self.assertIn("welcome: sent 2026-10-07", out)
        for label in ("Pause", "Skip next", "POPLoad installed", "POPLoad in use", "Mark done"):
            self.assertIn(label, out)
