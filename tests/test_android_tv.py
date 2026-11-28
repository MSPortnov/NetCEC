import asyncio
import itertools
import threading
import time
import unittest
from unittest import mock

from support import SettingsCase, main
from test_discovery import Server, desc
from test_tv_logic import FakeClock

ADB = {**main.DEFAULTS, "ip": "192.168.0.93", "driver": "adb", "adb_ok": True, "hdmi_port": 4, "wake_tv": True,
       "mac": "aa:bb:cc:dd:ee:ff"}
TCL_USER_IDS = {1: "HW1413744128", 2: "HW1413744384", 3: "HW1413744640", 4: "HW1413745664"}  # given by the user


def look_output(power="Awake", starter=(), resumed="com.google.android.tvlauncher/.MainActivity"):
    return (f"mWakefulness={power}\n##starter\n" + "\n".join(starter) + "\n##resumed\n"
            + (f"  topResumedActivity=ActivityRecord{{1 u0 {resumed} t12}}\n" if resumed else ""))


class Parse(unittest.TestCase):
    HW = {hw: port for port, hw in TCL_USER_IDS.items()}

    def parse(self, text):
        return main._adb_parse_look(text, self.HW)

    def test_a_tv_that_is_off(self):
        for state in ("Asleep", "Dozing"):
            self.assertIsNone(self.parse(look_output(power=state))[0], state)

    def test_home_screen_and_screensaver_are_something_else(self):
        self.assertEqual(self.parse(look_output())[0], 0)
        self.assertEqual(self.parse(look_output(power="Dreaming", resumed="com.android.systemui/.dreams.Dream"))[0], 0)

    def test_an_hdmi_input_is_told_by_the_last_opened_input(self):
        for port, hw in TCL_USER_IDS.items():
            text = look_output(starter=[hw], resumed="com.tcl.tvinput/.passthroughinput.PlayActivity")
            self.assertEqual(self.parse(text)[0], port, hw)

    def test_the_most_recent_input_wins(self):
        text = look_output(starter=[TCL_USER_IDS[2], TCL_USER_IDS[4]], resumed="com.tcl.tvinput/.X")
        self.assertEqual(self.parse(text)[0], 2)

    def test_an_input_that_is_not_in_the_table_is_unknown(self):
        self.assertEqual(self.parse(look_output(starter=["HW999"], resumed="com.tcl.tvinput/.X"))[0], -1)

    def test_an_input_without_any_trace_of_which_one_is_unknown(self):
        self.assertEqual(self.parse(look_output(starter=[], resumed="com.tcl.tvinput/.X"))[0], -1)

    def test_an_answer_without_power_state_is_unknown_not_off(self):
        self.assertEqual(self.parse("garbage")[0], -1)


class Packaging(unittest.TestCase):
    """The ADB client has to travel with the plugin, and has to load in Decky's Python."""

    def test_the_client_is_loaded_by_main(self):
        self.assertIsNotNone(main.netcec_adb)

    def test_the_client_is_part_of_the_install_and_the_release(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for path in ("deploy.sh", os.path.join(".github", "workflows", "release.yml")):
            with open(os.path.join(root, path), encoding="utf-8") as f:
                self.assertIn("py_modules", f.read(), path)


class Table(unittest.TestCase):
    def test_the_inputs_of_the_tcl_as_the_user_measured_them(self):
        self.assertEqual(main.ANDROID_TV_INPUTS["tcl"]["hw"], TCL_USER_IDS)

    def test_the_link_is_exactly_the_users(self):
        uri = main.ANDROID_TV_INPUTS["tcl"]["uri"].format(hw=TCL_USER_IDS[1])
        self.assertEqual(uri, "content://android.media.tv/passthrough/com.tcl.tvinput%2F.passthroughinput.TvPassThroughService%2FHW1413744128")


class Shell:
    """Stands in for _adb_shell: scripted outputs, records commands."""

    def __init__(self, brand="TCL", look=None, fail_on=None):
        self.commands, self.brand = [], brand
        self.look = look if look is not None else look_output()
        self.fail_on = fail_on

    def __call__(self, s, command, timeout=4.0):
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            raise OSError("boom")
        if command.startswith("getprop"):
            return f"{self.brand}\n{self.brand}\n"
        if "##starter" in command:  # the combined look (it also starts with "dumpsys power")
            return self.look
        if command.startswith("dumpsys power"):
            return "mWakefulness=Awake\n"
        if command.startswith("am start"):
            return "Starting: Intent { act=android.intent.action.VIEW dat=content://... }\n"
        return ""


class Commands(unittest.TestCase):
    def setUp(self):
        main._adb_state["brand"].clear()

    def run_with(self, fn, *args, **shell_args):
        sh = Shell(**shell_args)
        with mock.patch.object(main, "_adb_shell", sh), mock.patch.object(main, "_log", lambda m: None):
            result = fn(*args)
        return result, sh

    def test_switch_on_a_tcl_opens_the_input_link(self):
        for port, hw in TCL_USER_IDS.items():
            _, sh = self.run_with(main._adb_switch, ADB, port)
            expected = ('am start -a android.intent.action.VIEW -d "content://android.media.tv/passthrough/'
                        f'com.tcl.tvinput%2F.passthroughinput.TvPassThroughService%2F{hw}"')
            self.assertEqual(sh.commands[-1], expected, port)

    def test_other_brands_get_the_standard_hdmi_keys(self):
        for port, code in main.GENERIC_HDMI_KEYCODES.items():
            _, sh = self.run_with(main._adb_switch, ADB, port, brand="Xiaomi")
            self.assertEqual(sh.commands[-1], f"input keyevent {code}")
        self.assertEqual(main.GENERIC_HDMI_KEYCODES, {1: 243, 2: 244, 3: 245, 4: 246})

    def test_an_unsupported_port_is_reported(self):
        with self.assertRaises(RuntimeError):
            self.run_with(main._adb_switch, ADB, 7, brand="Xiaomi")

    def test_an_error_from_the_tv_is_an_error(self):
        sh = Shell()
        sh.__class__ = type("Bad", (Shell,), {"__call__": lambda self, s, c, t=4.0: (
            "Error: Activity not started, unable to resolve Intent\n" if c.startswith("am start")
            else Shell.__call__(self, s, c, t))})
        with mock.patch.object(main, "_adb_shell", sh), self.assertRaises(RuntimeError):
            main._adb_switch(ADB, 1)

    def test_back_can_go_with_the_switch_in_one_line(self):
        _, sh = self.run_with(main._adb_go, ADB, 2, True)
        self.assertTrue(sh.commands[-1].startswith("input keyevent 4 & am start -a android.intent.action.VIEW -d "))
        self.assertTrue(sh.commands[-1].endswith("; wait"))
        self.assertIn(TCL_USER_IDS[2], sh.commands[-1])
        _, sh = self.run_with(main._adb_go, ADB, 2, False)
        self.assertTrue(sh.commands[-1].startswith("am start"))  # without Back: the switch alone

    def test_back_goes_with_other_brands_keys_too(self):
        _, sh = self.run_with(main._adb_go, ADB, 3, True, brand="Xiaomi")
        self.assertEqual(sh.commands[-1], "input keyevent 4 & input keyevent 245; wait")

    def test_an_error_in_the_combined_line_is_still_an_error(self):
        sh = Shell()
        sh.__class__ = type("Bad", (Shell,), {"__call__": lambda self, s, c, t=4.0: (
            "Error: Activity not started\n" if "am start" in c else Shell.__call__(self, s, c, t))})
        with mock.patch.object(main, "_adb_shell", sh), self.assertRaises(RuntimeError):
            main._adb_go(ADB, 1, True)

    def test_the_brand_is_asked_once_per_tv(self):
        _, sh = self.run_with(lambda: [main._adb_switch(ADB, 1), main._adb_switch(ADB, 2)])
        self.assertEqual(sum(c.startswith("getprop") for c in sh.commands), 1)

    def test_power_state(self):
        for out, expected in (("mWakefulness=Awake", "active"), ("mWakefulness=Dreaming", "active"),
                              ("mWakefulness=Asleep", "standby"), ("mWakefulness=Dozing", "standby")):
            with mock.patch.object(main, "_adb_shell", lambda s, c, t=4.0, o=out: o):
                self.assertEqual(main._adb_power_state(ADB), expected, out)
        with mock.patch.object(main, "_adb_shell", lambda s, c, t=4.0: ""), self.assertRaises(RuntimeError):
            main._adb_power_state(ADB)

    def test_look_uses_the_brands_table_and_survives_an_unreachable_tv(self):
        text = look_output(starter=[TCL_USER_IDS[4]], resumed="com.tcl.tvinput/.X")
        (port, _), _ = self.run_with(main._adb_look, ADB, 3, look=text)
        self.assertEqual(port, 4)
        with mock.patch.object(main, "_adb_shell", side_effect=OSError("down")), mock.patch.object(main, "_log", lambda m: None):
            self.assertIsNone(main._adb_look(ADB, 3)[0])

    def test_unknown_brand_cannot_tell_the_port(self):
        text = look_output(starter=[TCL_USER_IDS[4]], resumed="com.some.tvinput/.X")
        (port, _), _ = self.run_with(main._adb_look, ADB, 3, look=text, brand="Xiaomi")
        self.assertEqual(port, -1)


class Wrappers(unittest.TestCase):
    def setUp(self):
        main._adb_state["brand"].clear()
        self.sh = Shell()
        patches = [mock.patch.object(main, "_adb_shell", self.sh), mock.patch.object(main, "_log", lambda m: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_the_keys_of_an_android_tv(self):
        main._t_back(ADB)
        main._t_home(ADB)
        main._t_off(ADB)
        self.assertEqual(self.sh.commands, ["input keyevent 4", "input keyevent 3", "input keyevent 223"])

    def test_wake_presses_power_on_and_sends_wake_on_lan_and_never_raises(self):
        with mock.patch.object(main, "_wol") as wol:
            main._t_wake(ADB)
            wol.assert_called_once()
        self.assertEqual(self.sh.commands, ["input keyevent 224"])
        self.sh.fail_on = "keyevent"
        with mock.patch.object(main, "_wol", side_effect=OSError):
            main._t_wake(ADB)  # both fail: still no exception

    def test_a_sony_tv_is_not_touched_by_the_adb_code(self):
        sony = {**ADB, "driver": "sony", "auth_cookie": "auth=x"}
        with mock.patch.object(main, "_ircc") as ircc, mock.patch.object(main, "_rest") as rest:
            main._t_back(sony)
            main._t_home(sony)
            main._t_off(sony)
        self.assertEqual([c.args[1] for c in ircc.call_args_list], [main.IRCC_BACK, main.IRCC_HOME])
        self.assertEqual(rest.call_args.args[3], [{"status": False}])
        self.assertEqual(self.sh.commands, [])

    def test_only_sony_can_confirm_the_input(self):
        self.assertFalse(main._t_verifies(ADB))
        self.assertTrue(main._t_verifies({**ADB, "driver": "sony"}))


class FakeAdbTv:
    """An Android TV in time: asleep until woken, then on its home screen, then on the HDMI input that was opened."""

    def __init__(self, asleep=False, wake_delay=2.0, port=0, reachable=True):
        self.clock, self.asleep, self.wake_delay, self.port, self.reachable = FakeClock(), asleep, wake_delay, port, reachable
        self.wake_at, self.commands = None, []

    def shell(self, s, command, timeout=4.0):
        if not self.reachable:
            raise OSError("unreachable")
        if self.asleep and self.wake_at is not None and self.clock.now >= self.wake_at:
            self.asleep, self.port = False, 0
        self.commands.append(command)
        if command.startswith("getprop"):
            return "TCL\nTCL\n"
        if "keyevent 224" in command:
            self.wake_at = self.clock.now + self.wake_delay
            return ""
        if "keyevent 223" in command:
            self.asleep = True
            return ""
        if "am start" in command:  # also inside "input keyevent 4 & am start ...; wait"
            for port, hw in TCL_USER_IDS.items():
                if hw in command:
                    self.port = port
            return "Starting: Intent { ... }\n"
        if "##starter" in command:  # the combined look (it also starts with "dumpsys power")
            if self.asleep:
                return look_output(power="Asleep")
            if self.port:
                return look_output(starter=[TCL_USER_IDS[self.port]], resumed="com.tcl.tvinput/.passthroughinput.X")
            return look_output()
        if command.startswith("dumpsys power"):
            return f"mWakefulness={'Asleep' if self.asleep else 'Awake'}\n"
        return ""

    def run(self, fn, *args):
        with mock.patch.object(main, "_adb_shell", self.shell), mock.patch.object(main, "_log", lambda m: None), \
                mock.patch.object(main, "_wol", lambda s: self.commands.append("wol")), \
                mock.patch.object(main, "_learn_mac", lambda s: None), \
                mock.patch.object(main, "_rediscover_ip", return_value=None), \
                mock.patch.object(main, "time", self.clock):
            return fn(*args)


class AndroidTvWakesUp(unittest.TestCase):
    def setUp(self):
        main._stop.clear()
        main._adb_state["brand"].clear()

    def test_a_tv_on_its_home_screen_gets_back_and_the_input_in_one_command(self):
        tv = FakeAdbTv(port=0)
        r = tv.run(main._tv_fast, dict(ADB))
        self.assertTrue(r["ok"])
        self.assertIn("HDMI 4", r["message"])
        self.assertEqual(tv.port, 4)
        goes = [c for c in tv.commands if "am start" in c]
        self.assertEqual(len(goes), 1)
        # Back runs in the background of the same shell line, at the same moment as the input is opened
        self.assertTrue(goes[0].startswith("input keyevent 4 & am start"), goes[0])
        self.assertIn(TCL_USER_IDS[4], goes[0])
        self.assertTrue(goes[0].endswith("; wait"))
        self.assertNotIn("wol", tv.commands)  # an awake TV is never sent wake commands
    def test_the_switch_is_not_waited_for(self):
        tv = FakeAdbTv(port=0)
        tv.run(main._tv_fast, dict(ADB))
        self.assertLess(tv.clock.now, 1)  # one short pause, no 8 s confirmation window

    def test_an_awake_tv_gets_the_command_whatever_it_shows(self):
        # an Android TV cannot tell its input precisely, so nothing is asked: the command goes (harmless on the port)
        tv = FakeAdbTv(port=4)
        r = tv.run(main._tv_fast, dict(ADB))
        self.assertTrue(r["ok"])
        self.assertEqual(len([c for c in tv.commands if "am start" in c]), 1)
    def test_a_sleeping_tv_is_woken_and_then_switched(self):
        tv = FakeAdbTv(asleep=True, wake_delay=3.0)
        r = tv.run(main._tv_fast, dict(ADB))
        self.assertTrue(r["ok"])
        self.assertEqual(tv.port, 4)
        sent = [c for c in tv.commands if c.startswith("input keyevent 224") or c == "wol" or "am start" in c]
        self.assertEqual(sent[:2], ["input keyevent 224", "wol"])  # woken first: a sleeping TV would swallow the command
        self.assertEqual(len(sent), 3)
        self.assertIn("am start", sent[2])
        self.assertTrue(sent[2].startswith("input keyevent 4 & "))  # Back goes with the command, once
    def test_another_port_is_switched_to_the_users_port(self):
        for want in (1, 2, 3):
            tv = FakeAdbTv(port=4)
            tv.run(main._tv_fast, {**ADB, "hdmi_port": want})
            self.assertEqual(tv.port, want)

    def test_a_tv_that_never_answers_ends_without_hanging(self):
        tv = FakeAdbTv(reachable=False)
        r = tv.run(main._tv_fast, dict(ADB))
        self.assertFalse(r["ok"])
        self.assertLess(tv.clock.now, main.WAKE_WINDOW + 5)


class AndroidTvLeaves(unittest.TestCase):
    def leave(self, mode, look_results):
        looks = list(look_results)
        sent = []
        with mock.patch.object(main, "_adb_look", lambda s, t=4: (looks.pop(0), "x")), \
                mock.patch.object(main, "_adb_shell", lambda s, c, t=4.0: sent.append(c) or ""), \
                mock.patch.object(main, "_log", lambda m: None):
            return main._leave(dict(ADB), mode), sent

    def test_on_my_port_the_action_runs(self):
        _, sent = self.leave("off", [4])
        self.assertEqual(sent, ["input keyevent 223"])
        _, sent = self.leave("home", [4])
        self.assertEqual(sent, ["input keyevent 3"])

    def test_another_input_or_the_home_screen_is_left_alone(self):
        for shown in (0, 2):
            _, sent = self.leave("off", [shown])
            self.assertEqual(sent, [], shown)

    def test_an_input_that_cannot_be_told_is_left_alone_and_says_so(self):
        result, sent = self.leave("off", [-1])
        self.assertEqual(sent, [])
        self.assertIn(main._t("what_undetectable"), result["message"])

    def test_a_tv_that_does_not_answer_is_asked_twice_then_left_alone(self):
        result, sent = self.leave("off", [None, None])
        self.assertEqual(sent, [])
        self.assertTrue(result["ok"])


class Pairing(SettingsCase):
    def setUp(self):
        super().setUp()
        main._adb_state.update(conn=None, ip=None)
        self.key = {"n": 1, "e": 1, "d": 1}
        for p in (mock.patch.object(main, "_adb_key", lambda: self.key), mock.patch.object(main, "_learn_mac", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def connection_class(self, behaviour):
        class Fake:
            def __init__(self, host, key, port=5555, timeout=4.0):
                self.args = (host, port)

            def connect(self, approval_wait=0.0):
                self.approval_wait = approval_wait
                behaviour(self)
                return self

            def close(self):
                pass

        return Fake

    def test_the_user_accepts_the_key_on_the_tv(self):
        waits = []
        with mock.patch.object(main.netcec_adb, "Connection", self.connection_class(lambda c: waits.append(c.approval_wait))):
            r = main._adb_pair({**ADB, "adb_ok": False})
        self.assertTrue(r["ok"])
        self.assertEqual(waits, [main.ADB_APPROVAL_WAIT])  # it waited for the user
        s = main._load()
        self.assertEqual((s["driver"], s["adb_ok"]), ("adb", True))

    def test_nobody_accepts(self):
        def refuse(c):
            raise main.netcec_adb.NeedsApproval("x")
        with mock.patch.object(main.netcec_adb, "Connection", self.connection_class(refuse)):
            r = main._adb_pair({**ADB, "adb_ok": False})
        self.assertFalse(r["ok"])
        self.assertEqual(r["message"], main._t("adb_approve"))
        self.assertFalse(main._load()["adb_ok"])

    def test_no_connection(self):
        def down(c):
            raise main.netcec_adb.AdbError("no connection")
        with mock.patch.object(main.netcec_adb, "Connection", self.connection_class(down)):
            self.assertFalse(main._adb_pair({**ADB, "adb_ok": False})["ok"])

    def test_without_the_adb_client_the_user_is_told_and_sony_is_not_affected(self):
        with mock.patch.object(main, "netcec_adb", None), mock.patch.object(main, "_ADB_IMPORT_ERROR", "no module"):
            r = main._adb_pair({**ADB, "adb_ok": False})
            self.assertIn("no module", r["message"])
            with self.assertRaises(RuntimeError):
                main._adb_shell(ADB, "x")

    def test_an_address_that_is_no_sony_but_has_adb_is_paired_as_an_android_tv(self):
        s = {**main.DEFAULTS, "ip": "192.168.0.93"}
        with mock.patch.object(main, "_register", side_effect=OSError("refused")), \
                mock.patch.object(main, "_port_open", return_value=True), \
                mock.patch.object(main, "_adb_pair", return_value={"ok": True, "message": "paired"}) as pair:
            r = main._pair_start(s)
        self.assertTrue(r["ok"])
        self.assertEqual(s["driver"], "adb")
        pair.assert_called_once()

    def test_an_address_that_answers_nothing_is_just_an_error(self):
        s = {**main.DEFAULTS, "ip": "192.168.0.93"}
        with mock.patch.object(main, "_register", side_effect=OSError("refused")), mock.patch.object(main, "_port_open", return_value=False):
            self.assertFalse(main._pair_start(s)["ok"])
        self.assertEqual(s["driver"], "sony")

    def test_paired_means_the_right_key_for_the_kind_of_tv(self):
        self.assertTrue(main._paired({**main.DEFAULTS, "ip": "x", "auth_cookie": "c"}))
        self.assertFalse(main._paired({**main.DEFAULTS, "ip": "x", "adb_ok": True}))  # Sony needs its own key
        self.assertTrue(main._paired({**main.DEFAULTS, "ip": "x", "driver": "adb", "adb_ok": True}))
        self.assertFalse(main._paired({**main.DEFAULTS, "ip": "x", "driver": "adb", "auth_cookie": "c"}))
        self.assertFalse(main._paired({**main.DEFAULTS, "driver": "adb", "adb_ok": True}))

    def test_unpairing_resets_everything_and_the_sony_driver_again(self):
        main._save({**ADB})
        asyncio.run(main.Plugin().tv_unpair())
        s = main._load()
        self.assertEqual((s["ip"], s["adb_ok"], s["driver"]), ("", False, "sony"))


class Status(unittest.TestCase):
    def status(self, look, adb_ok=True):
        with mock.patch.object(main, "_adb_look", lambda s, t=4: look), mock.patch.object(main, "_log", lambda m: None):
            return main._status({**ADB, "adb_ok": adb_ok})

    def test_states(self):
        self.assertFalse(self.status((0, ""), adb_ok=False)["ok"])
        self.assertIn("HDMI 4", self.status((4, "hdmi 4"))["message"])
        self.assertIn(main._t("st_standby"), self.status((None, "asleep (Asleep)"))["message"])
        self.assertFalse(self.status((None, "no answer (OSError)"))["ok"])
        self.assertTrue(self.status((-1, "?"))["ok"])


class Connections(unittest.TestCase):
    """One connection is reused for a short time; a dead one is replaced once."""

    class Conn:
        made = []

        def __init__(self, host, key, port=5555, timeout=4.0):
            self.alive, self.calls, self.dies_after = True, 0, None
            Connections.Conn.made.append(self)

        def connect(self, approval_wait=0.0):
            return self

        def shell(self, command, timeout=4.0):
            self.calls += 1
            if self.dies_after is not None and self.calls > self.dies_after:
                self.alive = False
                raise OSError("connection reset")
            return f"ran {command}"

        def close(self):
            self.alive = False

    def setUp(self):
        Connections.Conn.made = []
        main._adb_state.update(conn=None, ip=None, used=0.0)
        for p in (mock.patch.object(main.netcec_adb, "Connection", Connections.Conn), mock.patch.object(main, "_adb_key", lambda: {})):
            p.start()
            self.addCleanup(p.stop)

    def test_two_commands_share_one_connection(self):
        main._adb_shell(ADB, "a")
        main._adb_shell(ADB, "b")
        self.assertEqual(len(self.Conn.made), 1)

    def test_a_connection_that_died_is_replaced_once_and_the_command_still_runs(self):
        main._adb_shell(ADB, "a")
        self.Conn.made[0].dies_after = 1
        self.assertEqual(main._adb_shell(ADB, "b"), "ran b")
        self.assertEqual(len(self.Conn.made), 2)

    def test_a_fresh_connection_that_fails_is_not_retried(self):
        class Broken(self.Conn):
            def shell(self, command, timeout=4.0):
                raise OSError("no")

        with mock.patch.object(main.netcec_adb, "Connection", Broken):
            with self.assertRaises(OSError):
                main._adb_shell(ADB, "a")
            self.assertEqual(len(self.Conn.made), 1)

    def test_an_old_connection_is_not_reused(self):
        main._adb_shell(ADB, "a")
        main._adb_state["used"] -= main.ADB_IDLE + 1
        main._adb_shell(ADB, "b")
        self.assertEqual(len(self.Conn.made), 2)

    def test_another_tv_gets_its_own_connection(self):
        main._adb_shell(ADB, "a")
        main._adb_shell({**ADB, "ip": "192.168.0.94"}, "a")
        self.assertEqual(len(self.Conn.made), 2)

    def test_dropping_never_waits_for_a_running_command(self):
        main._adb_shell(ADB, "a")
        started = threading.Event()

        def hold():
            with main._adb_lock:
                started.set()
                time.sleep(1.0)

        t = threading.Thread(target=hold)
        t.start()
        started.wait()
        t0 = time.monotonic()
        main._adb_drop()  # what the unload does
        self.assertLess(time.monotonic() - t0, 0.3)
        t.join()
        self.assertIsNone(main._adb_state["conn"])


class EndToEnd(SettingsCase):
    """main.py and the ADB client against a fake adbd on a real socket: pairing, then waking up."""

    def setUp(self):
        super().setUp()
        import test_adb_client as client
        look = look_output(starter=[], resumed="com.google.android.tvlauncher/.MainActivity")
        hw4 = TCL_USER_IDS[4]
        uri = ("content://android.media.tv/passthrough/com.tcl.tvinput%2F.passthroughinput.TvPassThroughService%2F" + hw4)
        self.tv = client.FakeAdbd(outputs={
            "getprop ro.product.manufacturer; getprop ro.product.brand": "TCL\nTCL\n",
            main._ADB_LOOK_CMD: look,
            "dumpsys power | grep -m1 mWakefulness=": "mWakefulness=Awake\n",
            f'input keyevent 4 & am start -a android.intent.action.VIEW -d "{uri}"; wait': "Starting: Intent { act=android.intent.action.VIEW }\n",
        })
        self.addCleanup(self.tv.close)
        main._adb_state.update(conn=None, ip=None, used=0.0, brand={})
        self.addCleanup(main._adb_drop)
        for p in (mock.patch.object(main, "ADB_PORT", self.tv.port), mock.patch.object(main, "_adb_key", lambda: client.key()),
                  mock.patch.object(main, "_learn_mac", lambda s: None), mock.patch.object(main, "_log", lambda m: None)):
            p.start()
            self.addCleanup(p.stop)
        self.settings = {**main.DEFAULTS, "ip": "127.0.0.1", "driver": "adb", "hdmi_port": 4, "wake_tv": True}

    def test_pairing_then_waking_up(self):
        threading.Timer(0.3, self.tv.approve.set).start()  # the user presses "Always allow" on the TV
        paired = main._pair_start(dict(self.settings))
        self.assertTrue(paired["ok"], paired)
        saved = main._load()
        self.assertEqual((saved["driver"], saved["adb_ok"]), ("adb", True))

        result = main._tv_fast({**self.settings, "adb_ok": True})
        self.assertTrue(result["ok"], result)
        self.assertIn("HDMI 4", result["message"])
        # an Android TV is asked whether it is awake first (a sleeping one would swallow the command) ...
        self.assertEqual(self.tv.commands[0], "dumpsys power | grep -m1 mWakefulness=")
        goes = [c for c in self.tv.commands if "am start" in c]
        self.assertEqual(len(goes), 1)  # Back and the input of port 4 in one line, at the same moment
        self.assertTrue(goes[0].startswith("input keyevent 4 & am start") and TCL_USER_IDS[4] in goes[0])

    def test_going_to_sleep_with_the_tv_on_another_input_leaves_it_alone(self):
        self.tv.known.append((__import__("test_adb_client").key()["n"], 65537))
        threading.Timer(0, lambda: None).start()
        r = main._leave({**self.settings, "adb_ok": True}, "off")
        self.assertTrue(r["ok"])
        self.assertNotIn("input keyevent 223", self.tv.commands)


class Discovery(unittest.TestCase):
    CAST = ('<root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
            "<deviceType>urn:dial-multiscreen-org:device:dial:1</deviceType>"
            "<friendlyName>Living room</friendlyName><manufacturer>TCL</manufacturer><modelName>65C645</modelName>"
            "</device></root>")

    def setUp(self):
        self.srv = Server({"/cast.xml": self.CAST, "/router.xml": desc("Netcraze", "Gateway"),
                           "/sony.xml": desc("Sony", "BRAVIA 4K UR3", "TV")})
        self.addCleanup(self.srv.close)
        p = mock.patch.object(main, "_log", lambda m: None)
        p.start()
        self.addCleanup(p.stop)

    def found(self, path, adb_open=True, **kw):
        with mock.patch.object(main, "_ssdp_search", lambda timeout: {"127.0.0.1": [self.srv.url(path)]}), \
                mock.patch.object(main, "_port_open", return_value=adb_open):
            return main._discover()

    def test_an_android_tv_with_network_debugging_is_listed(self):
        self.assertEqual(self.found("/cast.xml"), [{"ip": "127.0.0.1", "name": "Living room", "model": "65C645", "kind": "adb"}])

    def test_without_network_debugging_it_is_not_listed(self):
        self.assertEqual(self.found("/cast.xml", adb_open=False), [])

    def test_a_sony_is_a_sony_even_if_the_adb_port_is_open(self):
        self.assertEqual([t["kind"] for t in self.found("/sony.xml")], ["sony"])

    def test_other_devices_are_not_android_tvs(self):
        self.assertEqual(self.found("/router.xml"), [])

    def test_without_the_adb_client_android_tvs_are_not_offered(self):
        with mock.patch.object(main, "netcec_adb", None):
            self.assertEqual(self.found("/cast.xml"), [])


if __name__ == "__main__":
    unittest.main()
