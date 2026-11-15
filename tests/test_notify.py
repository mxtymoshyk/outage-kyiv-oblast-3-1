"""Regression tests: every scenario the bot handles, no network, no Telegram.

Run: python3 -m unittest discover -s tests -v
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import notify  # noqa: E402

KYIV = notify.KYIV
TODAY = dt.date(2026, 11, 16)
TOMORROW = TODAY + dt.timedelta(days=1)


def day(off=(), first=(), second=(), maybe=()):
    """Hour slots 1..24 (slot h = h-1:00..h:00). first/second: outage only in that half."""
    out = {}
    for h in range(1, 25):
        out[str(h)] = ("no" if h in off else "first" if h in first else
                       "second" if h in second else "maybe" if h in maybe else "yes")
    return out


# 09:30–16:00 and 22:30–24:00
BASE_TODAY = day(off=range(11, 17), second=(10, 23)) | {"24": "no"}
# 00:00–03:00 and 14:00–18:00
BASE_TOMORROW = day(off=(1, 2, 3, 15, 16, 17, 18))


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.src_file = os.path.join(d, "src.json")
        self.patches = [
            mock.patch.object(notify, "STATE_FILE", os.path.join(d, "state.json")),
            mock.patch.object(notify, "ICS_FILE", os.path.join(d, "outage.ics")),
            mock.patch.object(notify, "send", side_effect=self._send),
            mock.patch.dict(os.environ, {"SOURCE_FILE": self.src_file}, clear=False),
        ]
        for p in self.patches:
            p.start()
        for k in ("TG_TOKEN", "TG_CHAT_ID", "GITHUB_EVENT_NAME", "FORCE", "TEST_NOW"):
            os.environ.pop(k, None)
        self.sent, self.deliver = [], True

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _send(self, text, now, photo=None):
        if self.deliver:
            self.sent.append(text)
        return self.deliver

    def run_bot(self, at, days, group="GPV3.1", updated=None, update_stamp="15.11.2026 20:28", **env):
        """days: {date: slots}. Returns messages sent in this run."""
        now = dt.datetime.fromisoformat(at).replace(tzinfo=KYIV)
        data = {}
        for d, slots in days.items():
            ts = int(dt.datetime.combine(d, dt.time(), KYIV).timestamp())
            data[str(ts)] = {group: slots}
        src = {
            "lastUpdated": (updated or now).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "lastUpdateStatus": {"ok": True, "code": 200},
            "fact": {"data": data, "update": update_stamp},
        }
        with open(self.src_file, "w", encoding="utf-8") as f:
            json.dump(src, f)
        before = len(self.sent)
        with mock.patch.dict(os.environ, {"TEST_NOW": at, **env}):
            try:
                notify.main()
            except SystemExit:
                pass
        return self.sent[before:]

    def state(self):
        with open(notify.STATE_FILE, encoding="utf-8") as f:
            return json.load(f)

    # ---------- schedule messages ----------
    def test_first_run_sends_days_without_news_line(self):
        msgs = self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("📅 <b>Сьогодні, пн 16.11</b> · без світла 8 год"))
        self.assertIn("🔴 09:30–16:00, 22:30–24:00", msgs[0])

    def test_unchanged_schedule_sends_nothing(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        self.assertEqual(self.run_bot("2026-11-16T07:15", {TODAY: BASE_TODAY}), [])

    def test_manual_run_without_changes_has_no_fake_news(self):
        # regression: used to say "Оновлено можливі відключення / немає"
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        msgs = self.run_bot("2026-11-16T07:15", {TODAY: BASE_TODAY}, GITHUB_EVENT_NAME="workflow_dispatch")
        self.assertEqual(len(msgs), 1)
        self.assertTrue(msgs[0].startswith("📅 <b>Сьогодні"))
        self.assertNotIn("можливі", msgs[0])

    def test_external_trigger_with_force_false_stays_quiet(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        self.assertEqual(self.run_bot("2026-11-16T07:15", {TODAY: BASE_TODAY},
                                      GITHUB_EVENT_NAME="workflow_dispatch", FORCE="false"), [])
        self.assertEqual(len(self.run_bot("2026-11-16T07:30", {TODAY: BASE_TODAY},
                                          GITHUB_EVENT_NAME="workflow_dispatch", FORCE="true")), 1)

    def test_new_tomorrow_news_on_top_days_in_order(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        msg, = self.run_bot("2026-11-16T20:10", {TODAY: BASE_TODAY, TOMORROW: BASE_TOMORROW})
        self.assertTrue(msg.startswith("🆕 <b>Графік на завтра</b> · без світла 7 год"))
        self.assertLess(msg.index("📅 <b>Сьогодні"), msg.index("📅 <b>Завтра"))
        self.assertIn("📅 <b>Завтра, вт 17.11</b> 🆕", msg)
        self.assertIn("🔴 00:00–03:00, 14:00–18:00", msg)

    def test_change_explains_in_outcome_words(self):
        self.run_bot("2026-11-16T06:00", {TODAY: BASE_TODAY})
        changed = day(off=range(9, 16))  # 08:00–15:00 ... ends at 15:00
        changed["16"] = "first"           # ... + 15:00–15:30
        msg, = self.run_bot("2026-11-16T06:30", {TODAY: changed})   # before the 08:00 reminder window
        self.assertTrue(msg.startswith("✏️ <b>Змінився графік на сьогодні</b> · без світла на 30 хв менше"))
        self.assertIn("🔴 08:00–15:30", msg)
        self.assertIn("↳ тепер без світла: 08:00–09:30", msg)
        self.assertIn("↳ тепер зі світлом: 15:30–16:00, 22:30–24:00", msg)

    def test_maybe_only_change(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        msg, = self.run_bot("2026-11-16T07:30", {TODAY: BASE_TODAY | {"20": "maybe"}})
        self.assertTrue(msg.startswith("🟡 <b>Оновлено можливі відключення</b>"))
        self.assertIn("🟡 Можливо: 19:00–20:00", msg)

    def test_day_without_outages(self):
        msg, = self.run_bot("2026-11-16T07:00", {TODAY: day()})
        self.assertIn("без відключень 🟢", msg)

    def test_external_text_is_escaped(self):
        msg, = self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY}, update_stamp="<b>x")
        self.assertIn("&lt;b&gt;x", msg)

    # ---------- reminders ----------
    def test_reminder_once_before_outage(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        msgs = self.run_bot("2026-11-16T08:40", {TODAY: BASE_TODAY})
        self.assertEqual(msgs, ["⏰ <b>О 09:30 вимкнуть світло</b> · через 50 хв\nДо 16:00 · 6,5 год без світла"])
        self.assertEqual(self.run_bot("2026-11-16T08:55", {TODAY: BASE_TODAY}), [])

    def test_reminder_spans_midnight(self):
        days = {TODAY: BASE_TODAY, TOMORROW: BASE_TOMORROW}
        self.run_bot("2026-11-16T20:00", days)
        msg, = self.run_bot("2026-11-16T21:40", days)
        self.assertIn("О 22:30 вимкнуть світло", msg)
        self.assertIn("До 03:00 завтра · 4,5 год без світла", msg)

    # ---------- health ----------
    def test_stale_source_alerts_once_then_recovers(self):
        old = dt.datetime(2026, 11, 16, 2, 0, tzinfo=KYIV)
        self.assertEqual(self.run_bot("2026-11-16T06:00", {TODAY: BASE_TODAY}, updated=old)[1:], [])
        alert = [m for m in self.run_bot("2026-11-16T07:05", {TODAY: BASE_TODAY}, updated=old) if "⚠️" in m]
        self.assertEqual(len(alert), 1)
        self.assertIn("дані не оновлювались", alert[0])
        again = self.run_bot("2026-11-16T07:20", {TODAY: BASE_TODAY}, updated=old)
        self.assertFalse(any("⚠️" in m for m in again))
        back = self.run_bot("2026-11-16T07:35", {TODAY: BASE_TODAY})
        self.assertTrue(any(m.startswith("✅") for m in back))

    def test_missing_group_is_a_problem(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY}, group="GPV9.9")
        msgs = self.run_bot("2026-11-16T08:05", {TODAY: BASE_TODAY}, group="GPV9.9")
        self.assertTrue(any("немає графіка для черги 3.1" in m for m in msgs))

    # ---------- delivery failures ----------
    def test_failed_delivery_is_retried_next_run(self):
        self.deliver = False
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY})
        self.assertEqual(self.state().get("days"), {})
        self.deliver = True
        self.assertEqual(len(self.run_bot("2026-11-16T07:15", {TODAY: BASE_TODAY})), 1)

    # ---------- calendar ----------
    def test_ics_glues_outage_across_midnight(self):
        self.run_bot("2026-11-16T07:00", {TODAY: BASE_TODAY, TOMORROW: BASE_TOMORROW})
        with open(notify.ICS_FILE, encoding="utf-8", newline="") as f:   # keep CRLF
            ics = f.read()
        # 22:30 Kyiv (UTC+2) = 20:30Z, 03:00 next day = 01:00Z
        self.assertIn("DTSTART:20261116T203000Z\r\nDTEND:20261117T010000Z", ics)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 3)


class OwnPictureTest(unittest.TestCase):
    @unittest.skipUnless(notify.import_pillow(), "Pillow not installed (only needed for the fallback)")
    def test_own_drawing_is_a_png(self):
        png = notify.render_image({TODAY: notify.half_hours(BASE_TODAY),
                                   TOMORROW: notify.half_hours(BASE_TOMORROW)}, TODAY, "15.11.2026 20:28")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
