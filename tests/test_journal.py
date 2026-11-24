import asyncio
import json
import os
import unittest
from unittest import mock

from support import SettingsCase, main


class Journal(SettingsCase):
    """The journal while it is switched on (it is off by default)."""

    def setUp(self):
        super().setUp()
        main._update(lambda s: s.update(journal=True))

    def test_newest_first_and_capped(self):
        for i in range(main.MAX_EVENTS + 5):
            main._record("wake", True, f"event {i}")
        events = main._read_events()
        self.assertEqual(len(events), main.MAX_EVENTS)
        self.assertEqual(events[0]["text"], f"event {main.MAX_EVENTS + 4}")

    def test_an_entry_has_time_kind_result_and_text(self):
        main._record("sleep", False, "x" * 500)
        e = main._read_events()[0]
        self.assertEqual((e["kind"], e["ok"]), ("sleep", False))
        self.assertEqual(len(e["text"]), 200)  # long texts are cut
        self.assertGreater(e["t"], 1_000_000_000)

    def test_a_damaged_journal_is_not_a_problem(self):
        with open(main._events_file(), "w") as f:
            f.write("{ not json")
        main._record("wake", True, "after the damage")
        self.assertEqual([e["text"] for e in main._read_events()], ["after the damage"])

    def test_recording_never_raises(self):
        with mock.patch.object(main.os, "replace", side_effect=OSError("disk full")):
            main._record("wake", True, "x")  # must not raise: the journal is not worth breaking an action

    def test_the_ui_gets_the_five_newest(self):
        for i in range(8):
            main._record("wake", True, str(i))
        events = asyncio.run(main.Plugin().get_events())
        self.assertEqual([e["text"] for e in events], ["7", "6", "5", "4", "3"])

    def test_nothing_to_do_and_a_reboot_leave_no_entry(self):
        main._record_result("wake", {"ok": True, "message": "Nothing to do", "empty": True})
        main._record_result("shutdown", {"ok": True, "message": "Reboot: TV left on", "quiet": True})
        main._record_result("wake", None)
        self.assertEqual(main._read_events(), [])

    def test_the_plain_text_is_preferred(self):
        main._record_result("wake", {"ok": True, "message": "TV: Switched", "plain": "Switched"})
        self.assertEqual(main._read_events()[0]["text"], "Switched")

    def test_the_journal_is_a_file_of_its_own_next_to_the_settings(self):
        main._record("wake", True, "x")
        self.assertEqual(os.path.dirname(main._events_file()), os.path.dirname(main.SETTINGS_FILE))
        with open(main._events_file()) as f:
            self.assertIsInstance(json.load(f), list)


class JournalSwitch(SettingsCase):
    """"Action journal": off by default; while it is off nothing is recorded or shown."""

    def events_file_exists(self):
        return os.path.exists(main._events_file())

    def test_it_is_off_by_default(self):
        self.assertIs(main._load()["journal"], False)
        self.assertIs(main.DEFAULTS["journal"], False)

    def test_nothing_is_recorded_while_it_is_off(self):
        main._record("wake", True, "x")
        main._record_result("sleep", {"ok": True, "message": "y"})
        self.assertFalse(self.events_file_exists())

    def test_nothing_is_shown_while_it_is_off_even_if_a_file_is_there(self):
        with open(main._events_file(), "w") as f:
            json.dump([{"t": 1_700_000_000, "kind": "wake", "ok": True, "text": "old"}], f)
        self.assertEqual(asyncio.run(main.Plugin().get_events()), [])

    def test_switching_it_on_starts_recording_and_showing(self):
        asyncio.run(main.Plugin().set_settings({"journal": True}))
        main._record("wake", True, "now")
        self.assertEqual([e["text"] for e in asyncio.run(main.Plugin().get_events())], ["now"])

    def test_switching_it_off_deletes_what_was_recorded(self):
        plugin = main.Plugin()
        asyncio.run(plugin.set_settings({"journal": True}))
        main._record("wake", True, "now")
        self.assertTrue(self.events_file_exists())
        asyncio.run(plugin.set_settings({"journal": False}))
        self.assertFalse(self.events_file_exists())
        main._record("wake", True, "after")  # and nothing is written any more
        self.assertFalse(self.events_file_exists())

    def test_other_changes_do_not_touch_the_journal(self):
        main._update(lambda s: s.update(journal=True))
        main._record("wake", True, "kept")
        asyncio.run(main.Plugin().set_settings({"lang_pref": "ru"}))
        self.assertTrue(self.events_file_exists())

    def test_only_a_real_switch_value_is_accepted(self):
        self.assertIs(main._sanitize({"journal": "yes"})["journal"], False)
        self.assertIs(main._sanitize({"journal": True})["journal"], True)


class ResumeAll(SettingsCase):
    def run_all(self, tv, usb=None):
        with mock.patch.object(main, "_tv_actions", return_value=tv), mock.patch.object(main, "_apply_wake_rules", return_value=usb):
            return asyncio.run(main._resume_all())

    def test_nothing_configured_is_empty(self):
        self.assertTrue(self.run_all(None)["empty"])

    def test_the_tv_result_is_kept_as_plain_text(self):
        r = self.run_all({"ok": True, "message": "Switched to HDMI 4 in 4.8 s"})
        self.assertEqual(r["plain"], "Switched to HDMI 4 in 4.8 s")
        self.assertFalse(r["empty"])

    def test_a_failed_part_makes_the_whole_result_failed(self):
        r = self.run_all({"ok": True, "message": "fine"}, {"ok": False, "message": "USB failed"})
        self.assertFalse(r["ok"])
        self.assertIn("USB failed", r["plain"])


class Texts(unittest.TestCase):
    """The agreed names in the panel."""

    def source(self):
        with open(os.path.join(os.path.dirname(main.__file__), "src", "index.tsx"), encoding="utf-8") as f:
            return f.read()

    def test_the_journal_has_a_switch_with_its_name(self):
        s = self.source()
        self.assertIn('journal: "Журнал действий"', s)
        self.assertIn('journal: "Action journal"', s)
        self.assertIn("{s.journal && (", s)  # the list is behind the switch

    def test_wake_sources_are_not_called_usb(self):
        s = self.source()
        self.assertIn('sec_usb: "Источники пробуждения",', s)
        self.assertIn('sec_usb: "Wake sources",', s)
        self.assertNotIn("(USB)", s)  # a Bluetooth adapter is in that list too


class NoNotifications(unittest.TestCase):
    """Nothing is pushed to the Steam UI: the user sees the result on the TV and in the journal."""

    def test_the_backend_never_emits_events(self):
        with open(os.path.join(os.path.dirname(main.__file__), "main.py"), encoding="utf-8") as f:
            self.assertNotIn("decky.emit", f.read())

    def test_the_frontend_has_no_pop_ups(self):
        with open(os.path.join(os.path.dirname(main.__file__), "src", "index.tsx"), encoding="utf-8") as f:
            source = f.read()
        for word in ("toaster", "addEventListener", "resume_result"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
