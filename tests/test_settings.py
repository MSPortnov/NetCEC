import json
import os
import stat
import threading
import unittest

from support import SettingsCase, main


class SaveAndLoad(SettingsCase):
    def test_first_start_gives_defaults(self):
        self.assertEqual(main._load(), main.DEFAULTS)

    def test_roundtrip(self):
        main._save({**main.DEFAULTS, "ip": "192.168.1.95", "hdmi_port": 3})
        s = main._load()
        self.assertEqual((s["ip"], s["hdmi_port"]), ("192.168.1.95", 3))

    def test_file_is_private_and_no_temp_file_is_left(self):
        main._save({**main.DEFAULTS, "auth_cookie": "auth=SECRET"})
        self.assertEqual(stat.S_IMODE(os.stat(main.SETTINGS_FILE).st_mode), 0o600)
        self.assertFalse(os.path.exists(main.SETTINGS_FILE + ".tmp"))

    def test_backup_keeps_the_previous_version(self):
        main._save({**main.DEFAULTS, "ip": "10.0.0.1"})
        main._save({**main.DEFAULTS, "ip": "10.0.0.2"})
        with open(main.SETTINGS_FILE + ".bak") as f:
            self.assertEqual(json.load(f)["ip"], "10.0.0.1")
        self.assertEqual(stat.S_IMODE(os.stat(main.SETTINGS_FILE + ".bak").st_mode), 0o600)


class DamagedFile(SettingsCase):
    def _pair(self):
        main._save({**main.DEFAULTS, "ip": "192.168.1.95", "auth_cookie": "auth=SECRET"})
        main._save({**main.DEFAULTS, "ip": "192.168.1.95", "auth_cookie": "auth=SECRET", "lang_pref": "ru"})

    def _damage(self):
        with open(main.SETTINGS_FILE) as f:
            raw = f.read()
        with open(main.SETTINGS_FILE, "w") as f:
            f.write(raw[: len(raw) // 2])  # a half-written file

    def test_pairing_survives_a_damaged_file(self):
        self._pair()
        self._damage()
        s = main._load()
        self.assertEqual((s["ip"], s["auth_cookie"]), ("192.168.1.95", "auth=SECRET"))
        # the next change from the UI must not wipe it either
        main._update(lambda cur: cur.update(lang_pref="en"))
        self.assertEqual(main._load()["auth_cookie"], "auth=SECRET")

    def test_damaged_file_is_kept_for_inspection(self):
        self._pair()
        self._damage()
        main._load()
        self.assertTrue(os.path.exists(main.SETTINGS_FILE + ".corrupt"))

    def test_damaged_file_without_backup_gives_defaults_but_is_not_overwritten(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(main.SETTINGS_FILE, "w") as f:
            f.write("{ not json")
        self.assertEqual(main._load(), main.DEFAULTS)
        self.assertTrue(os.path.exists(main.SETTINGS_FILE + ".corrupt"))

    def test_not_a_json_object(self):
        with open(main.SETTINGS_FILE, "w") as f:
            f.write("[1, 2, 3]")
        self.assertEqual(main._load(), main.DEFAULTS)


class Validation(SettingsCase):
    def test_invalid_values_become_defaults(self):
        s = main._sanitize({"hdmi_port": "abc", "suspend_action": "bogus", "wake_tv": "yes",
                            "ip": "evil.example.com/x?", "lang": "de", "mac": "nonsense"})
        d = main.DEFAULTS
        for key in ("hdmi_port", "suspend_action", "wake_tv", "ip", "lang", "mac"):
            self.assertEqual(s[key], d[key], key)

    def test_valid_values_are_kept(self):
        s = main._sanitize({"hdmi_port": 4, "suspend_action": "home", "shutdown_action": "off",
                            "wake_tv": False, "ip": "tv.local", "lang_pref": "ru",
                            "mac": "aa:bb:cc:dd:ee:ff"})
        self.assertEqual((s["hdmi_port"], s["suspend_action"], s["wake_tv"], s["ip"]), (4, "home", False, "tv.local"))

    def test_booleans_are_not_accepted_as_numbers(self):
        self.assertEqual(main._sanitize({"hdmi_port": True})["hdmi_port"], main.DEFAULTS["hdmi_port"])

    def test_wake_rules_keep_only_valid_entries(self):
        rules = main._sanitize({"wake_rules": {"046d:c52b": True, "bad": True, "1234:abcd": "yes"}})["wake_rules"]
        self.assertEqual(rules, {"046d:c52b": True})

    def test_unknown_keys_are_dropped(self):
        self.assertNotIn("junk", main._sanitize({"junk": 1}))

    def test_host_check(self):
        for good in ("192.168.1.95", "tv.local", "bravia", "my-tv.example.com"):
            self.assertTrue(main._host_ok(good), good)
        for bad in ("", "192.168.", "300.1.1.1", "a/b", "tv.local/x", "user@tv", "tv local", "tv?x", "a" * 300, None):
            self.assertFalse(main._host_ok(bad), repr(bad))

    def test_half_typed_ip_can_be_stored(self):
        # the UI saves while the user types; "192.168." must not be rejected (and not silently kept stale)
        self.assertEqual(main._sanitize({"ip": "192.168."})["ip"], "192.168.")


class PluginApi(SettingsCase):
    def _run(self, coro):
        import asyncio
        return asyncio.run(coro)

    def test_set_settings_ignores_bad_and_private_keys(self):
        plugin = main.Plugin()
        self._run(plugin.set_settings({"hdmi_port": "abc", "auth_cookie": "auth=HACK", "mac": "aa:bb:cc:dd:ee:ff",
                                       "suspend_action": "bogus", "wake_tv": False}))
        s = main._load()
        self.assertEqual(s["hdmi_port"], main.DEFAULTS["hdmi_port"])
        self.assertEqual(s["auth_cookie"], "")
        self.assertEqual(s["mac"], "")
        self.assertEqual(s["suspend_action"], main.DEFAULTS["suspend_action"])
        self.assertFalse(s["wake_tv"])  # the valid one was applied

    def test_get_settings_never_exposes_the_pairing_key(self):
        main._save({**main.DEFAULTS, "ip": "10.0.0.5", "auth_cookie": "auth=SECRET", "mac": "aa:bb:cc:dd:ee:ff"})
        out = self._run(main.Plugin().get_settings())
        self.assertNotIn("auth_cookie", out)
        self.assertNotIn("mac", out)
        self.assertTrue(out["paired"])

    def test_unpair_clears_everything_about_the_tv(self):
        main._save({**main.DEFAULTS, "ip": "10.0.0.5", "auth_cookie": "auth=SECRET", "mac": "aa:bb:cc:dd:ee:ff"})
        self._run(main.Plugin().tv_unpair())
        s = main._load()
        self.assertEqual((s["ip"], s["auth_cookie"], s["mac"]), ("", "", ""))

    def test_usb_set_rejects_odd_ids(self):
        self._run(main.Plugin().usb_set("../../etc", True))
        self.assertEqual(main._load()["wake_rules"], {})


class Migration(SettingsCase):
    """Older files are converted step by step.
    Before 1.3.8 (no settings_version): two TV switches became one.
    Before 1.3.9 (version 2): "do nothing" and the separate "only if on my port" switch went away.
    Before 1.3.10 (version 3): "turn off if on my port" is the only way: both actions act only if the TV shows
    our HDMI port, so sleep and shutdown offer exactly two actions: home screen / turn off."""

    def load(self, **values):
        with open(main.SETTINGS_FILE, "w") as f:
            json.dump({"ip": "10.0.0.5", **values}, f)
        return main._load()

    def test_a_new_install_starts_at_the_current_version(self):
        s = main._load()
        self.assertEqual(s["settings_version"], 5)
        self.assertEqual((s["suspend_action"], s["shutdown_action"]), ("off", "off"))

    # --- the first change (wake and switch merged)
    def test_switch_only_becomes_wake_and_switch(self):
        self.assertTrue(self.load(wake_tv=False, switch_input=True)["wake_tv"])

    def test_neither_stays_off(self):
        self.assertFalse(self.load(wake_tv=False, switch_input=False)["wake_tv"])

    # --- the second change ("do nothing" is gone)
    def test_do_nothing_becomes_turn_off(self):
        for key in ("suspend_action", "shutdown_action"):
            for old in ("none", None):  # None: the key is missing
                data = {key: old} if old else {}
                self.assertEqual(self.load(settings_version=2, **data)[key], "off", (key, old))

    def test_home_stays_home(self):
        for flag in (True, False):
            s = self.load(settings_version=2, suspend_action="home", off_only_if_active=flag)
            self.assertEqual(s["suspend_action"], "home", flag)

    def test_off_with_or_without_the_old_condition_is_off(self):
        for flag in (True, False):
            s = self.load(settings_version=2, suspend_action="off", off_only_if_active=flag, shutdown_action="off")
            self.assertEqual((s["suspend_action"], s["shutdown_action"]), ("off", "off"), flag)

    # --- the third change (the condition is part of every action)
    def test_off_if_mine_becomes_off(self):
        s = self.load(settings_version=3, suspend_action="off_if_mine", shutdown_action="off_if_mine")
        self.assertEqual((s["suspend_action"], s["shutdown_action"]), ("off", "off"))

    def test_version_3_home_and_off_are_kept(self):
        s = self.load(settings_version=3, suspend_action="home", shutdown_action="off")
        self.assertEqual((s["suspend_action"], s["shutdown_action"]), ("home", "off"))

    def test_a_file_without_a_version_goes_through_all_steps(self):
        s = self.load(shutdown_action="off", off_only_if_active=True, suspend_action="none", wake_tv=False, switch_input=True)
        self.assertEqual((s["shutdown_action"], s["suspend_action"], s["wake_tv"]), ("off", "off", True))

    def test_the_removed_keys_are_gone_after_a_save(self):
        self.load(settings_version=2, off_only_if_active=True, switch_input=True)
        main._update(lambda s: None)
        with open(main.SETTINGS_FILE) as f:
            saved = json.load(f)
        self.assertNotIn("off_only_if_active", saved)
        self.assertNotIn("switch_input", saved)
        self.assertEqual(saved["settings_version"], 5)

    def test_old_files_get_the_sony_driver(self):
        s = self.load(settings_version=4, suspend_action="home")
        self.assertEqual((s["driver"], s["adb_ok"]), ("sony", False))

    def test_the_driver_is_checked(self):
        self.assertEqual(main._sanitize({"driver": "adb"})["driver"], "adb")
        self.assertEqual(main._sanitize({"driver": "telnet"})["driver"], "sony")

    def test_the_adb_flag_cannot_be_set_from_the_ui(self):
        import asyncio
        asyncio.run(main.Plugin().set_settings({"adb_ok": True, "driver": "adb"}))
        s = main._load()
        self.assertEqual((s["driver"], s["adb_ok"]), ("adb", False))

    def test_a_migrated_file_is_not_migrated_again(self):
        self.load(settings_version=3, suspend_action="off_if_mine")
        main._update(lambda s: s.update(suspend_action="home"))  # the user picks "home" afterwards
        self.assertEqual(main._load()["suspend_action"], "home")

    def test_action_values(self):
        for ok in main.LEAVE_ACTIONS:
            self.assertEqual(main._sanitize({"suspend_action": ok, "shutdown_action": ok})["suspend_action"], ok)
        for bad in ("none", "off_if_mine", "", None):
            self.assertEqual(main._sanitize({"suspend_action": bad})["suspend_action"], "off", bad)


class Concurrency(SettingsCase):
    def test_parallel_updates_do_not_lose_each_other(self):
        def worker(n):
            for i in range(25):
                main._update(lambda s, key=f"{n:04x}:{i:04x}": s.update(wake_rules={**s["wake_rules"], key: True}))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(main._load()["wake_rules"]), 6 * 25)

    def test_pairing_renewal_and_ui_change_do_not_overwrite_each_other(self):
        main._save({**main.DEFAULTS, "ip": "10.0.0.5", "auth_cookie": "auth=OLD"})
        s = main._load()
        t = threading.Thread(target=lambda: [main._store_cookie(s, f"auth=NEW{i}") for i in range(30)])
        t.start()
        for i in range(30):
            main._update(lambda cur, i=i: cur.update(hdmi_port=1 + i % 4))
        t.join()
        final = main._load()
        self.assertEqual(final["auth_cookie"], "auth=NEW29")  # the renewed key was not lost
        self.assertIn(final["hdmi_port"], (1, 2, 3, 4))


if __name__ == "__main__":
    unittest.main()
