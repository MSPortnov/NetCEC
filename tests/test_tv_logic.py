import itertools
import threading
import unittest
from unittest import mock

from support import main

BASE = {**main.DEFAULTS, "ip": "192.168.1.95", "auth_cookie": "auth=x", "mac": "aa:bb:cc:dd:ee:ff",
        "hdmi_port": 2, "wake_tv": True}


class FakeClock:
    """Time that only moves when the code sleeps: the tests run instantly and can count the waiting."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.now += seconds
        self.sleeps.append(seconds)

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now


class FakeTv:
    """A TV modelled in time, like the real one:
    - it shows `initial` until a switch command takes effect; every accepted command (re)starts the
      switching, which needs `delay` seconds before the TV reports the new port (before that it answers
      with an error, as the real TV does: "Illegal State");
    - `ignore_commands`: the first N commands are lost;
    - `first_looks`: scripted answers for the first looks (None = no answer at all), e.g. a sleeping TV."""

    def __init__(self, power=(), initial=0, delay=1.0, ignore_commands=0, first_looks=(), home_fails=False,
                 lives_at=None, fail_first_switches=0):
        self.power = iter(power)
        self.initial, self.delay, self.ignore = initial, delay, ignore_commands
        self.first_looks = list(first_looks)
        self.home_fails = home_fails
        self.lives_at = lives_at  # the address the TV answers on (None: any)
        self.fail_switches = fail_first_switches  # a TV in standby refuses the first commands
        self.sent, self.commands, self.effective_at = [], 0, None
        self.clock = FakeClock()

    def rest(self, s, service, method, params=None, timeout=4):
        if self.lives_at and s["ip"] != self.lives_at:
            raise OSError("unreachable")
        self.sent.append(method)
        if method == "setPlayContent":
            if self.fail_switches > 0:
                self.fail_switches -= 1
                raise RuntimeError([40005, "Display Is Turned off"])
            self.commands += 1
            if self.commands > self.ignore:
                self.effective_at = self.clock.now + self.delay
        return {}

    def ircc(self, s, code, timeout=3):
        if self.lives_at and s["ip"] != self.lives_at:
            raise OSError("unreachable")
        self.sent.append("KEY")
        if self.home_fails:
            raise RuntimeError("ircc failed")

    def look(self, s, timeout=4):
        if self.lives_at and s["ip"] != self.lives_at:
            return None, "no answer"
        if self.first_looks:
            port = self.first_looks.pop(0)
        elif self.effective_at is not None and self.clock.now >= self.effective_at:
            port = BASE["hdmi_port"]
        else:
            port = self.initial
        return port, ("no answer" if port is None else f"port {port}")

    def power_state(self, s):
        if self.lives_at and s["ip"] != self.lives_at:
            raise OSError("unreachable")  # nothing answers at that address
        return next(self.power, "active")

    def run(self, settings=None):
        s = {**BASE, **(settings or {})}
        with mock.patch.object(main, "_rest", self.rest), mock.patch.object(main, "_ircc", self.ircc), \
                mock.patch.object(main, "_look", self.look), \
                mock.patch.object(main, "_power_state", self.power_state), \
                mock.patch.object(main, "_wol", lambda s_: self.sent.append("wol")), \
                mock.patch.object(main, "_learn_mac", lambda s_: None), \
                mock.patch.object(main, "_log", lambda m: None), \
                mock.patch.object(main, "time", self.clock):
            return main._tv_fast(s)

    @property
    def switches(self):
        return self.sent.count("setPlayContent")


class Look(unittest.TestCase):
    """What the TV's answer means: an error answer is NOT the same as no answer."""

    def ask(self, behaviour):
        with mock.patch.object(main, "_rest", behaviour):
            return main._look(BASE)

    def test_hdmi_input_is_reported(self):
        self.assertEqual(self.ask(lambda *a, **k: {"result": [{"uri": "extInput:hdmi?port=4"}]}), (4, "hdmi 4"))

    def test_something_else_is_zero(self):
        port, what = self.ask(lambda *a, **k: {"result": [{"uri": "com.sony.dtv.home"}]})
        self.assertEqual(port, 0)
        self.assertIn("com.sony.dtv.home", what)

    def test_an_error_answer_means_the_tv_is_on_and_plays_nothing(self):
        def error_answer(*a, **k):
            raise RuntimeError([7, "Illegal State"])
        port, what = self.ask(error_answer)
        self.assertEqual(port, 0)
        self.assertIn("Illegal State", what)

    def test_no_connection_is_unknown(self):
        def no_answer(*a, **k):
            raise OSError("timed out")
        self.assertIsNone(self.ask(no_answer)[0])

    def test_lost_pairing_is_not_swallowed(self):
        def refused(*a, **k):
            raise main.AuthError("401")
        with self.assertRaises(main.AuthError):
            self.ask(refused)


class TvFast(unittest.TestCase):
    def setUp(self):
        main._stop.clear()  # another test may have set the unload flag

    def test_a_tv_that_is_already_on_the_port_gets_the_command_too_and_confirms_at_once(self):
        # nothing is asked first: asking costs time on every wake-up, and the command is harmless there
        tv = FakeTv(initial=2)
        r = tv.run()
        self.assertTrue(r["ok"])
        self.assertEqual(sorted(tv.sent), ["KEY", "setPlayContent"])
        self.assertEqual(tv.clock.sleeps, [main.CONFIRM_STEP])
    def test_a_tv_that_is_on_is_never_sent_wake_commands(self):
        tv = FakeTv(initial=0, delay=0.3)  # home screen / screensaver: "something else", not "no answer"
        tv.run()
        self.assertNotIn("setPowerStatus", tv.sent)
        self.assertNotIn("wol", tv.sent)

    def test_screensaver_or_home_screen_gets_the_key_and_one_switch(self):
        tv = FakeTv(initial=0, delay=0.3)
        self.assertTrue(tv.run()["ok"])
        self.assertEqual(sorted(tv.sent), ["KEY", "setPlayContent"])

    def test_the_key_and_the_switch_go_out_at_the_same_moment(self):
        barrier = threading.Barrier(2, timeout=2)  # both calls must be in flight together, or this test fails

        class Together(FakeTv):
            def rest(self, s, service, method, params=None, timeout=4):
                if method == "setPlayContent":
                    barrier.wait()
                return super().rest(s, service, method, params, timeout)

        tv = Together(initial=0, delay=0.3)
        tv.ircc = lambda s, code, timeout=3: (barrier.wait(), tv.sent.append("KEY"))
        self.assertTrue(tv.run()["ok"])
        self.assertEqual(sorted(tv.sent), ["KEY", "setPlayContent"])

    def test_nothing_waits_before_the_command(self):
        tv = FakeTv(initial=0, delay=0.3)
        tv.run()
        self.assertEqual(tv.clock.sleeps[0], main.CONFIRM_STEP)  # the first thing the code waits for is the TV's answer
        self.assertFalse(hasattr(main, "KEY_PAUSE"))
    def test_waiting_is_short_when_the_tv_switches_at_once(self):
        tv = FakeTv(initial=0, delay=0.3)
        tv.run()
        self.assertEqual(tv.clock.sleeps, [main.CONFIRM_STEP])
    def test_a_slow_tv_is_waited_for_without_sending_the_command_again(self):
        # seen on the real TV: it reports the new input only after 3 to 9 seconds
        for delay in (3.2, 6.0, 7.5):
            tv = FakeTv(initial=0, delay=delay)
            r = tv.run()
            self.assertTrue(r["ok"], delay)
            self.assertEqual(tv.switches, 1, delay)
            self.assertIn("HDMI 2", r["message"], delay)

    def test_a_busy_tv_that_does_not_answer_is_not_taken_for_success(self):
        tv = FakeTv(initial=0, delay=1.0, first_looks=[0, None, None])
        self.assertTrue(tv.run()["ok"])
        self.assertEqual(tv.switches, 1)

    def test_a_command_that_got_lost_is_sent_once_more_after_the_whole_window(self):
        tv = FakeTv(initial=0, delay=0.5, ignore_commands=1)
        r = tv.run()
        self.assertTrue(r["ok"])
        self.assertIn("HDMI 2", r["message"])
        self.assertEqual(tv.switches, 2)
        self.assertEqual(tv.sent.count("KEY"), 1)  # the key only once

    def test_unconfirmed_is_reported_as_sent_not_as_failed(self):
        tv = FakeTv(initial=0, ignore_commands=99)  # the TV never switches
        r = tv.run()
        self.assertTrue(r["ok"])
        self.assertEqual(tv.switches, main.SWITCH_SENDS)
        self.assertEqual(r["message"], main._t("sent_unconfirmed", port=2))
        self.assertLess(tv.clock.now, 25)  # and it does not take forever

    def test_failed_key_press_does_not_block_the_switch(self):
        tv = FakeTv(initial=0, delay=0.3, home_fails=True)
        self.assertTrue(tv.run()["ok"])
        self.assertEqual(sorted(tv.sent), ["KEY", "setPlayContent"])
    def test_a_sleeping_tv_refuses_the_command_is_woken_and_then_switched(self):
        tv = FakeTv(power=["standby", "active"], fail_first_switches=1, delay=0.3)
        r = tv.run()
        self.assertTrue(r["ok"])
        first_try = tv.sent.index("setPlayContent")
        wake = tv.sent.index("setPowerStatus")
        last_try = len(tv.sent) - 1 - tv.sent[::-1].index("setPlayContent")
        self.assertLess(first_try, wake)  # the command went first ...
        self.assertLess(wake, last_try)   # ... the TV was woken, and the command went again
        self.assertIn("wol", tv.sent)
        self.assertEqual(tv.sent.count("KEY"), 2)  # the key goes with every try until a command took
    def test_a_tv_that_never_answers_gives_up_after_the_wake_window(self):
        tv = FakeTv(power=itertools.repeat("standby"), lives_at="10.9.9.9")  # nothing answers at the address
        r = tv.run()
        self.assertFalse(r["ok"])
        self.assertGreaterEqual(tv.clock.now, main.WAKE_WINDOW)
    def test_not_configured(self):
        self.assertFalse(FakeTv().run({"auth_cookie": ""})["ok"])
        self.assertFalse(FakeTv().run({"ip": ""})["ok"])
        self.assertFalse(FakeTv().run({"ip": "tv.local/x"})["ok"])  # never contact a mangled address


class BackKey(unittest.TestCase):
    """The key pressed before the switch ends a screensaver: Back, as the user chose."""

    def test_the_key_is_back_and_not_home(self):
        self.assertEqual(main.IRCC_BACK, "AAAAAgAAAJcAAAAjAw==")  # Sony's documented "Back" (Return)
        self.assertNotEqual(main.IRCC_BACK, main.IRCC_HOME)

    def test_that_code_is_what_is_actually_sent_before_the_switch(self):
        sent = []
        tv = FakeTv(initial=0, delay=0.3)
        tv.ircc = lambda s, code, timeout=3: sent.append(code)
        tv.run()
        self.assertEqual(sent, [main.IRCC_BACK])


class TvMovedToAnotherAddress(unittest.TestCase):
    """The router gave the TV a new address: it is found again by its MAC address and the setting is updated."""

    def setUp(self):
        main._stop.clear()

    def run_with(self, tv, found):
        records, updates = [], []
        with mock.patch.object(main, "_rediscover_ip", return_value=found) as rediscover, \
                mock.patch.object(main, "_update", lambda change: updates.append(change)), \
                mock.patch.object(main, "_record", lambda kind, ok, text: records.append((kind, ok, text))):
            result = tv.run()
        return result, rediscover, records, updates

    def test_the_tv_is_followed_to_its_new_address(self):
        tv = FakeTv(initial=0, delay=0.3, lives_at="192.168.1.96")
        result, rediscover, records, updates = self.run_with(tv, "192.168.1.96")
        self.assertTrue(result["ok"])
        self.assertEqual(rediscover.call_count, 1)
        self.assertGreaterEqual(tv.clock.now, main.HEAL_AFTER)  # not before the TV had a fair chance to answer
        self.assertEqual(len(updates), 1)  # the new address is saved
        self.assertEqual(records[0][:2], ("wake", True))
        self.assertIn("192.168.1.96", records[0][2])

    def test_the_search_is_tried_only_once_per_run(self):
        tv = FakeTv(power=itertools.repeat("standby"), lives_at="10.9.9.9")  # a TV that is simply off
        result, rediscover, records, updates = self.run_with(tv, None)
        self.assertFalse(result["ok"])
        self.assertEqual(rediscover.call_count, 1)
        self.assertEqual(updates, [])
    def test_a_tv_that_answers_is_never_searched_for(self):
        tv = FakeTv(initial=0, delay=0.3)
        _, rediscover, _, _ = self.run_with(tv, "192.168.1.96")
        rediscover.assert_not_called()


class RediscoverIp(unittest.TestCase):
    TVS = [{"ip": "192.168.1.95", "name": "TV", "model": "x"}, {"ip": "192.168.1.96", "name": "TV", "model": "x"},
           {"ip": "192.168.1.50", "name": "Other", "model": "y"}]
    MACS = {"192.168.1.95": "11:11:11:11:11:11", "192.168.1.96": "AA-BB-CC-DD-EE-FF", "192.168.1.50": "22:22:22:22:22:22"}

    def find(self, mac, tvs=None):
        with mock.patch.object(main, "_discover", return_value=tvs or self.TVS), \
                mock.patch.object(main, "_guess_mac", lambda ip: self.MACS.get(ip, "")):
            return main._rediscover_ip({**BASE, "mac": mac})

    def test_the_tv_with_our_mac_address_is_found_even_if_the_notation_differs(self):
        self.assertEqual(self.find("aa:bb:cc:dd:ee:ff"), "192.168.1.96")
        self.assertEqual(self.find("AA:BB:CC:DD:EE:FF"), "192.168.1.96")

    def test_a_different_tv_is_not_taken_for_ours(self):
        self.assertIsNone(self.find("99:99:99:99:99:99"))

    def test_without_a_known_mac_nothing_is_guessed(self):
        self.assertIsNone(self.find(""))

    def test_the_current_address_is_not_reported_as_new(self):
        self.assertIsNone(self.find("11:11:11:11:11:11"))


class TvActions(unittest.TestCase):
    def test_wake_switch_off_means_nothing_happens(self):
        with mock.patch.object(main, "_tv_fast", side_effect=AssertionError("must not be called")):
            self.assertIsNone(main._tv_actions({**BASE, "wake_tv": False}))

    def test_wake_switch_on_wakes_and_switches(self):
        with mock.patch.object(main, "_tv_fast", return_value={"ok": True, "message": "x"}) as fast:
            self.assertTrue(main._tv_actions(BASE)["ok"])
        fast.assert_called_once()

    def test_no_tv_configured(self):
        self.assertIsNone(main._tv_actions({**BASE, "ip": ""}))


if __name__ == "__main__":
    unittest.main()
