import unittest
from unittest import mock

from support import main

S = {**main.DEFAULTS, "ip": "192.168.1.95", "auth_cookie": "auth=x", "hdmi_port": 2}


class Recorder:
    """Stands in for the TV: scripted answers to "which input?", records the commands."""

    def __init__(self, looks=(), command_fails=False):
        self.looks, self.asked_with, self.sent = list(looks), [], []
        self.command_fails = command_fails

    def current_port(self, s, timeout=4):
        self.asked_with.append(timeout)
        return self.looks.pop(0) if self.looks else None

    def ircc(self, s, code, timeout=3):
        self.sent.append("home")
        if self.command_fails:
            raise RuntimeError("no")

    def rest(self, s, service, method, params=None, timeout=4):
        self.sent.append(f"{method}:{params[0]['status']}")
        if self.command_fails:
            raise RuntimeError("no")
        return {}

    def leave(self, mode):
        with mock.patch.object(main, "_current_port", self.current_port), mock.patch.object(main, "_ircc", self.ircc), \
                mock.patch.object(main, "_rest", self.rest), mock.patch.object(main, "_log", lambda m: None):
            return main._leave(S, mode)


class Leave(unittest.TestCase):
    """Nobody else's TV is ever touched: the action runs only if the TV shows our HDMI port."""

    def test_off_when_the_tv_shows_my_port(self):
        tv = Recorder(looks=[2])
        r = tv.leave("off")
        self.assertTrue(r["ok"])
        self.assertEqual(tv.sent, ["setPowerStatus:False"])

    def test_home_when_the_tv_shows_my_port(self):
        tv = Recorder(looks=[2])
        tv.leave("home")
        self.assertEqual(tv.sent, ["home"])

    def test_other_input_is_left_alone_for_both_actions(self):
        for mode in ("off", "home"):
            for shown in (0, 3):  # 0: home screen / an app; 3: another HDMI port
                tv = Recorder(looks=[shown])
                r = tv.leave(mode)
                self.assertTrue(r["ok"])
                self.assertEqual(tv.sent, [], (mode, shown))

    def test_a_slow_tv_is_asked_a_second_time(self):
        # the glitch from the log: the first question got no answer within 2 s, the TV was left on
        tv = Recorder(looks=[None, 2])
        tv.leave("home")
        self.assertEqual(tv.asked_with, list(main.LEAVE_LOOK_TIMEOUTS))
        self.assertEqual(tv.sent, ["home"])

    def test_a_tv_that_stays_silent_is_left_alone(self):
        tv = Recorder(looks=[None, None])
        r = tv.leave("off")
        self.assertTrue(r["ok"])
        self.assertEqual(tv.sent, [])
        self.assertEqual(len(tv.asked_with), 2)

    def test_an_answer_is_never_asked_twice(self):
        tv = Recorder(looks=[0, 2])
        tv.leave("off")
        self.assertEqual(len(tv.asked_with), 1)

    def test_a_failed_command_is_reported(self):
        self.assertFalse(Recorder(looks=[2], command_fails=True).leave("off")["ok"])


class SleepAndShutdown(unittest.TestCase):
    """Sleep and shutdown offer the same two actions; they always do one of them."""

    def call(self, fn, settings, *args):
        with mock.patch.object(main, "_leave", return_value={"ok": True, "message": "x"}) as leave:
            result = fn({**S, **settings}, *args)
        return result, leave

    def test_sleep_actions(self):
        for choice in main.LEAVE_ACTIONS:
            _, leave = self.call(main._tv_suspend, {"suspend_action": choice})
            leave.assert_called_once()
            self.assertEqual(leave.call_args.args[1:], (choice,), choice)

    def test_shutdown_actions(self):
        for choice in main.LEAVE_ACTIONS:
            _, leave = self.call(main._tv_shutdown, {"shutdown_action": choice}, "poweroff")
            leave.assert_called_once()
            self.assertEqual(leave.call_args.args[1:], (choice,), choice)

    def test_a_reboot_never_touches_the_tv(self):
        for choice in main.LEAVE_ACTIONS:
            result, leave = self.call(main._tv_shutdown, {"shutdown_action": choice}, "reboot")
            self.assertTrue(result["ok"])
            leave.assert_not_called()

    def test_no_tv_configured_means_nothing_to_do(self):
        for fn, args in ((main._tv_suspend, ()), (main._tv_shutdown, ("poweroff",))):
            result, leave = self.call(fn, {"ip": ""}, *args)
            self.assertIsNone(result)
            leave.assert_not_called()

    def test_there_are_exactly_two_actions_and_off_is_the_default(self):
        self.assertEqual(main.LEAVE_ACTIONS, ("home", "off"))
        self.assertEqual((main.DEFAULTS["suspend_action"], main.DEFAULTS["shutdown_action"]), ("off", "off"))


if __name__ == "__main__":
    unittest.main()
