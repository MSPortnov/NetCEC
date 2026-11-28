import asyncio
import threading
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


class CancelWake(SettingsCase):
    def test_sleep_and_poweroff_cancel_a_wake_that_is_waiting_for_the_tv(self):
        main._save({**main.DEFAULTS, "ip": "192.0.2.4", "driver": "adb", "adb_ok": True, "hdmi_port": 4})

        async def scenario(departure):
            plugin = main.Plugin()
            reading = threading.Event()
            release = threading.Event()

            def power_state(_settings):
                reading.set()
                if not release.wait(2):
                    raise AssertionError("the wake operation was not released")
                return "active"

            with mock.patch.object(main, "_t_power_state", side_effect=power_state), \
                    mock.patch.object(main, "_t_current_port", return_value=4), \
                    mock.patch.object(main, "_t_off") as off, \
                    mock.patch.object(main, "_t_go") as go:
                wake = asyncio.create_task(plugin._handle_resume("test"))
                try:
                    self.assertTrue(await asyncio.to_thread(reading.wait, 2))
                    if departure == "sleep":
                        await plugin.on_suspend()
                    else:
                        await plugin._handle_signal("PrepareForShutdownWithMetadata", [
                            "boolean true\n", 'string "type"\n', 'variant string "power-off"\n',
                        ])
                        await plugin._handle_signal("PrepareForShutdown", ["boolean true\n"])
                        self.assertIsNone(plugin._plain_task)
                finally:
                    release.set()
                    await wake
                off.assert_called_once()
                go.assert_not_called()

        for departure in ("sleep", "shutdown"):
            with self.subTest(departure=departure), mock.patch.object(main, "_suspend_count", return_value=1):
                asyncio.run(scenario(departure))

    def test_ps_works_after_aborted_steam_sleep_only_once_preparation_has_expired(self):
        main._save({**main.DEFAULTS, "ip": "192.0.2.4", "auth_cookie": "auth=test"})
        plugin = main.Plugin()
        plugin._ps_last_action = float("-inf")

        async def scenario():
            with mock.patch.object(main, "_tv_suspend", return_value=None), \
                    mock.patch.object(main, "_t_current_port", return_value=0), \
                    mock.patch.object(main, "_tv_fast", return_value=None) as wake:
                await plugin.on_suspend()
                old = plugin._wake_cancelled
                await plugin._on_ps_press()
                wake.assert_not_called()

                plugin._sleep_ts -= 11
                plugin._logind_sleeping = True
                await plugin._on_ps_press()
                wake.assert_not_called()

                plugin._logind_sleeping = False
                await plugin._on_ps_press()
                wake.assert_called_once()
                self.assertTrue(old.is_set())
                self.assertFalse(wake.call_args.args[1].is_set())

        asyncio.run(scenario())

    def test_a_logind_sleep_end_reenables_ps_even_if_the_kernel_count_did_not_change(self):
        plugin = main.Plugin()
        plugin._last_suspends = 2

        async def scenario():
            with mock.patch.object(main, "_suspend_count", return_value=2), \
                    mock.patch.object(main, "_tv_suspend", return_value=None), \
                    mock.patch.object(main, "_resume_all", new_callable=mock.AsyncMock) as resume:
                await plugin._on_logind("sleep", True)
                old = plugin._wake_cancelled
                await plugin._on_logind("sleep", False)
                await asyncio.gather(*plugin._bg)
                self.assertTrue(old.is_set())
                self.assertFalse(plugin._wake_cancelled.is_set())
                resume.assert_not_awaited()

        asyncio.run(scenario())


class SleepDetector(SettingsCase):
    def test_suspend_counter_detects_a_short_sleep_without_waiting_for_clock_threshold(self):
        plugin = main.Plugin()
        clock_samples = iter([(1.0, 1.0), (1.5, 1.5)])
        counts = iter([3, 4])
        sleep_calls = 0

        async def stop_after_one_poll(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        async def scenario():
            with mock.patch.object(main, "_clocks", side_effect=lambda: next(clock_samples)), \
                    mock.patch.object(main, "_suspend_count", side_effect=lambda: next(counts)), \
                    mock.patch.object(main.asyncio, "sleep", side_effect=stop_after_one_poll), \
                    mock.patch.object(plugin, "_handle_resume", new_callable=mock.AsyncMock) as resume:
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._watch_sleep()
                resume.assert_awaited_once_with("sleep-detector")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
