import asyncio
import unittest
from unittest import mock

from support import SettingsCase, main

OK = {"ok": True, "message": "m", "plain": "p", "empty": False}


class HandleResume(SettingsCase):
    """Every real wake-up is handled exactly once, however many of the three sources report it."""

    def setUp(self):
        super().setUp()
        main._last_resume = 0.0
        self.recorded = []
        for patch in (mock.patch.object(main, "_record", lambda kind, ok, text: self.recorded.append(kind)),
                      mock.patch.object(main, "_log", lambda m: None)):
            patch.start()
            self.addCleanup(patch.stop)

    def resumes(self, counts, **plugin_attrs):
        """Reports a resume for every value of the kernel counter and returns how many were handled."""
        plugin = main.Plugin()
        for k, v in plugin_attrs.items():
            setattr(plugin, k, v)
        handled = 0

        async def scenario():
            nonlocal handled
            with mock.patch.object(main, "_resume_all", mock.AsyncMock(return_value=OK)) as resume_all:
                for count in counts:
                    with mock.patch.object(main, "_suspend_count", return_value=count):
                        await plugin._handle_resume("test")
                handled = resume_all.call_count

        asyncio.run(scenario())
        return handled

    def test_three_sources_for_one_wake_up_handle_it_once(self):
        self.assertEqual(self.resumes([5, 5, 5], _last_suspends=4), 1)

    def test_a_second_real_wake_up_right_after_the_first_is_not_lost(self):
        # sleep, wake, sleep again at once, wake again: two attempts finished, two wake-ups (the 20 s timer lost one)
        self.assertEqual(self.resumes([5, 5, 6, 6], _last_suspends=4), 2)

    def test_a_failed_suspend_attempt_wakes_the_tv_again_too(self):
        # the TV was sent away on the way to sleep; the kernel counts a failed attempt as well
        self.assertEqual(self.resumes([5], _last_suspends=4), 1)

    def test_the_count_at_start_is_the_baseline(self):
        self.assertEqual(self.resumes([4, 4], _last_suspends=4), 0)  # nothing new happened since the plugin started

    def test_without_the_counter_the_20_second_timer_is_the_fallback(self):
        self.assertEqual(self.resumes([None, None, None]), 1)

    def test_the_result_goes_to_the_journal_once(self):
        self.resumes([5, 5], _last_suspends=4)
        self.assertEqual(self.recorded, ["wake"])


class SuspendCounter(unittest.TestCase):
    def test_success_and_fail_are_added(self):
        values = {"success": "7\n", "fail": "2\n"}

        def fake_open(path, *a, **k):
            return mock.mock_open(read_data=values[path.rsplit("/", 1)[1]])()

        with mock.patch("builtins.open", fake_open):
            self.assertEqual(main._suspend_count(), 9)

    def test_unavailable_is_none(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            self.assertIsNone(main._suspend_count())


if __name__ == "__main__":
    unittest.main()
