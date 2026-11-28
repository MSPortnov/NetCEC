"""Decky plugin API and lifecycle orchestration."""
import asyncio
import os
import re
import struct
import threading
import time



class _Runtime:
    """Live view of main.py globals without importing a colliding module named main."""
    def __init__(self, namespace):
        self.namespace = namespace

    def __getattr__(self, name):
        return self.namespace[name]


class Plugin:
    _runtime = None

    @classmethod
    def bind(cls, namespace):
        cls._runtime = _Runtime(namespace)
    _lock = None   # systemd-inhibit process holding the sleep/shutdown delay lock
    _lock_mutex = None  # makes taking the lock atomic (two quick calls must not start two)
    _mon = None    # dbus-monitor process (logind signals)
    _tasks = ()
    _sleep_fut = None   # sleep actions of the current sleep (shared by the Steam hook and logind)
    _sleep_ts = 0.0
    _last_suspends = None  # the kernel's count of finished suspend attempts at the last handled resume
    _plain_task = None
    _ps_files = {}  # read-only event-device file descriptors; observing does not grab the controller
    _ps_labels = {}
    _ps_seen_inputs = []
    _ps_hid_files = {}  # raw Sony HID reports bypass Steam's evdev grab; only the PS bit is inspected
    _ps_hid_labels = {}
    _ps_hid_state = {}
    _ps_last_action = 0.0
    _ps_debug_remaining = 0
    _ps_startup_done = False

    def __init__(self):
        self._bg = set()
        self._wake_cancelled = threading.Event()
        self._shutting_down = False
        self._logind_sleeping = False

    def _spawn(self, coro):
        t = asyncio.get_event_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    # ----- journal
    async def get_events(self) -> list:
        def read():
            return self._runtime._read_events()[:5] if self._runtime._load()["journal"] else []
        return await asyncio.to_thread(read)

    # ----- settings / info
    async def get_settings(self) -> dict:
        s = self._runtime._load()
        out = {k: v for k, v in s.items() if k not in self._runtime.PRIVATE}  # the UI never sees the pairing key
        out["paired"] = self._runtime._paired(s)
        out["sony_paired"] = bool(s["ip"] and s["auth_cookie"])
        out["adb_trained_ports"] = sorted(int(port) for port in s.get("adb_inputs", {}))
        return out

    async def set_settings(self, patch: dict) -> None:
        def apply(s):
            previous_ip = s["ip"]
            for k, v in (patch or {}).items():
                if k in self._runtime.DEFAULTS and k not in self._runtime.PRIVATE and self._runtime._VALID[k](v):
                    s[k] = v
                else:
                    self._runtime._log(f"settings: ignored {k!r}")
            if s["ip"] != previous_ip:
                s.update(auth_cookie="", adb_ok=False, mac="", adb_inputs={})
        await asyncio.to_thread(self._runtime._update, apply)
        if (patch or {}).get("journal") is False:
            await asyncio.to_thread(self._runtime._clear_events)  # switched off: nothing stays behind
        await self._take_lock()

    async def get_info(self) -> dict:
        return {
            "root": os.geteuid() == 0,
            "monitor": bool(self._mon and self._mon.returncode is None),
            "lock": bool(self._lock and self._lock.returncode is None),
        }

    # ----- TV
    async def find_tvs(self) -> list:
        return await asyncio.to_thread(self._runtime._discover)

    async def pair_start(self) -> dict:
        result = await asyncio.to_thread(self._runtime._pair_start, self._runtime._load())
        if result.get("ok"):
            await self._take_lock()
        return result

    async def train_hdmi(self) -> dict:
        return await asyncio.to_thread(self._runtime._adb_train_inputs, self._runtime._load())

    async def pair_finish(self, pin: str) -> dict:
        result = await asyncio.to_thread(self._runtime._pair_finish, self._runtime._load(), pin)
        if result.get("ok"):
            await self._take_lock()  # a TV is paired now: sleep and shutdown have something to do
        return result

    async def tv_status(self) -> dict:
        return await asyncio.to_thread(self._runtime._status, self._runtime._load())

    async def tv_unpair(self) -> dict:
        await asyncio.to_thread(self._runtime._adb_drop)
        await asyncio.to_thread(self._runtime._update, lambda s: s.update(
            ip="", auth_cookie="", mac="", adb_ok=False, adb_inputs={}, driver="sony"))
        await self._take_lock()  # nothing to do any more: release the lock
        return self._runtime._ok(self._runtime._t("unpaired"))

    # ----- USB wake sources
    async def usb_list(self) -> list:
        return await asyncio.to_thread(self._runtime._public_usb)

    async def usb_set(self, dev_id: str, enabled: bool) -> list:
        if isinstance(dev_id, str) and self._runtime._USB_ID_RE.fullmatch(dev_id):
            await asyncio.to_thread(self._runtime._update, lambda s: s.update(wake_rules={**s["wake_rules"], dev_id: bool(enabled)}))
            await asyncio.to_thread(self._runtime._usb_set, dev_id, bool(enabled))
        return await asyncio.to_thread(self._runtime._public_usb)

    # ----- sleep / shutdown lock (systemd delay inhibitor)
    async def _take_lock(self):
        if self._lock_mutex is None:
            self._lock_mutex = asyncio.Lock()
        async with self._lock_mutex:
            s = await asyncio.to_thread(self._runtime._load)
            need = self._runtime._paired(s)  # something to do for a paired TV
            if not need:
                await self._release_lock()
                return
            if self._lock and self._lock.returncode is None:
                return
            try:
                self._lock = await asyncio.create_subprocess_exec(
                    *self._runtime._exe(self._runtime.LOCK_ARGS), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=self._runtime._env(),
                    preexec_fn=self._runtime._pdeathsig, start_new_session=True)
                self._runtime._log("sleep/shutdown lock taken")
            except Exception as e:
                self._lock = None
                self._runtime._log(f"could not take lock: {e}")

    async def _release_lock(self):
        p, self._lock = self._lock, None
        if p and p.returncode is None:
            self._runtime._kill_group(p)  # no waiting for the exit: nothing here may block
            self._runtime._log("sleep/shutdown lock released")

    # ----- resume handling
    async def _handle_resume(self, source: str) -> dict:
        """Single entry for "the device woke up". Three sources report it: logind (the main one), the Steam UI
        event and the clock-jump detector (fallbacks). Each real wake-up is handled exactly once: the kernel
        counts finished suspend attempts, so a source that reports the same count again is a duplicate. Without
        the counter, duplicates within 20 s are dropped."""
        if self._shutting_down:
            return {**self._runtime._ok(self._runtime._t("nothing")), "skipped": True}
        count = self._runtime._suspend_count()
        if count is not None:
            duplicate = count == self._last_suspends
            self._last_suspends = count
        else:
            now = time.time()
            duplicate = now - self._runtime._last_resume < 20
            if not duplicate:
                self._runtime._last_resume = now
        if duplicate:
            if source == "logind" and self._wake_cancelled.is_set():
                self._sleep_fut, self._sleep_ts = None, 0.0
                self._wake_cancelled = threading.Event()
            self._runtime._log(f"resume ({source}) skipped: duplicate")
            return {**self._runtime._ok(self._runtime._t("dup")), "skipped": True}
        self._logind_sleeping = False
        self._sleep_fut, self._sleep_ts = None, 0.0  # a new sleep after this resume must not be skipped
        self._wake_cancelled.set()
        self._wake_cancelled = threading.Event()
        self._runtime._log(f"resume ({source})")
        res = await self._runtime._resume_all(self._wake_cancelled)
        self._runtime._record_result("wake", res)
        return res

    async def on_resume(self) -> dict:  # Steam UI event (fallback path)
        return await self._handle_resume("steam-event")

    async def _sleep_run(self, source: str, deadline: float):
        """Run configured TV actions before sleep."""
        s = self._runtime._load()
        try:
            tv = await asyncio.to_thread(self._runtime._tv_suspend, s, deadline=deadline)
        except Exception as e:
            self._runtime._log(f"sleep actions ({source}): tv error: {e}")
            return self._runtime._fail(str(e))
        self._runtime._log(f"sleep actions ({source}): tv={tv}")
        self._runtime._record_result("sleep", tv)
        return tv

    def _sleep_recent(self) -> bool:
        return self._sleep_fut is not None and time.monotonic() - self._sleep_ts < 10

    def _start_sleep(self, source: str):
        self._wake_cancelled.set()
        self._sleep_ts = time.monotonic()
        deadline = self._sleep_ts + self._runtime.SLEEP_BUDGET
        self._sleep_fut = asyncio.ensure_future(self._sleep_run(source, deadline))

    async def on_suspend(self, source: str = "steam-event") -> dict:
        """Steam UI announces the sleep BEFORE logind: the network is still up here, whereas
        NetworkManager shuts it down at the same moment logind signals PrepareForSleep."""
        if self._sleep_recent():
            self._runtime._log(f"suspend ({source}): skipped, already handled")
            return {**self._runtime._ok(self._runtime._t("dup")), "skipped": True}
        self._start_sleep(source)
        r = await self._sleep_fut
        return r or self._runtime._ok(self._runtime._t("nothing"))

    async def _on_logind(self, kind: str, going_down: bool, typ=None):
        self._runtime._log(f"logind: {kind} {'start' if going_down else 'end'}" + (f" (type={typ})" if typ else ""))
        if kind == "sleep":
            self._logind_sleeping = going_down
        if going_down:
            try:
                if kind == "sleep":
                    if self._sleep_recent():
                        self._runtime._log("sleep: actions already started by a Steam UI hook, waiting for them")
                    else:
                        self._start_sleep("logind")
                    remaining = self._sleep_ts + self._runtime.SLEEP_BUDGET - time.monotonic()
                    r = await asyncio.wait_for(asyncio.shield(self._sleep_fut), remaining)
                else:
                    self._shutting_down = True
                    self._wake_cancelled.set()
                    deadline = time.monotonic() + self._runtime.SLEEP_BUDGET
                    action = asyncio.to_thread(self._runtime._tv_shutdown, self._runtime._load(), typ, deadline=deadline)
                    r = await asyncio.wait_for(action, deadline - time.monotonic())
                    self._runtime._record_result("shutdown", r)
                self._runtime._log(f"{kind}: {r}")
            except asyncio.TimeoutError:
                self._runtime._log(f"{kind}: actions timed out")
            except Exception as e:
                self._runtime._log(f"{kind}: failed: {e}")
            finally:
                await self._release_lock()  # let logind continue
        elif kind == "sleep":
            await self._take_lock()
            self._spawn(self._handle_resume("logind"))
        elif self._shutting_down:
            self._shutting_down = False
            if not self._sleep_recent():
                self._wake_cancelled = threading.Event()
            await self._take_lock()

    async def _handle_signal(self, member: str, lines):
        text = "".join(lines)
        m = re.search(r"boolean\s+(true|false)", text)
        if not m:
            return
        start = m.group(1) == "true"
        if member == "PrepareForSleep":
            await self._on_logind("sleep", start)
        elif member == "PrepareForShutdownWithMetadata":
            tm = re.search(r'string\s+"type"\s+variant\s+string\s+"([^"]+)"', text)
            typ = tm.group(1) if tm else None
            if typ is None:
                self._runtime._log(f"shutdown metadata not parsed: {text!r}")
            if self._plain_task:
                self._plain_task.cancel()
            await self._on_logind("shutdown", start, typ)
        elif member == "PrepareForShutdown" and not start:
            if self._plain_task:
                self._plain_task.cancel()
            await self._on_logind("shutdown", False)
        elif member == "PrepareForShutdown" and not self._shutting_down:
            # logind sends ...WithMetadata first when supported, then this compatibility signal.
            self._shutting_down = True
            self._wake_cancelled.set()
            self._plain_task = self._spawn(self._plain_shutdown())

    async def _plain_shutdown(self):
        await asyncio.sleep(0.8)
        await self._on_logind("shutdown", True, None)

    async def _logind_loop(self):
        """Follow logind's PrepareForSleep / PrepareForShutdown(WithMetadata) via dbus-monitor."""
        while True:
            proc = None
            try:
                proc = self._mon = await asyncio.create_subprocess_exec(
                    *self._runtime._exe(self._runtime.MONITOR_ARGS), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, env=self._runtime._env(), preexec_fn=self._runtime._pdeathsig)
                self._runtime._log("logind watcher started")
                member, lines = None, []

                async def flush():
                    nonlocal member, lines
                    m, ls = member, lines
                    member, lines = None, []
                    if m:
                        await self._handle_signal(m, ls)

                while True:
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), 0.2)
                    except asyncio.TimeoutError:
                        await flush()  # the arguments of a signal have all arrived
                        continue
                    if not raw:
                        break
                    line = raw.decode(errors="replace")
                    if line.startswith("signal"):
                        await flush()
                        for name in ("PrepareForSleep", "PrepareForShutdownWithMetadata", "PrepareForShutdown"):
                            if f"member={name}" in line:
                                member = name
                                break
                    elif member:
                        lines.append(line)
                        if member == "PrepareForSleep" and "boolean" in line:
                            await flush()  # no further arguments: act immediately
                await flush()
                self._runtime._log("logind watcher ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._runtime._log(f"logind watcher error: {e}")
            finally:
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            await asyncio.sleep(5)

    async def _watch_sleep(self):
        """Fallback: the kernel counter catches short sleeps; clocks are used if it is unavailable."""
        boot, mono = self._runtime._clocks()
        suspend_count = self._runtime._suspend_count()
        while True:
            await asyncio.sleep(self._runtime.SLEEP_POLL)
            nboot, nmono = self._runtime._clocks()
            slept = (nboot - boot) - (nmono - mono)
            boot, mono = nboot, nmono
            current_count = self._runtime._suspend_count()
            counter_changed = (current_count is not None and suspend_count is not None
                               and current_count != suspend_count)
            if current_count is not None:
                suspend_count = current_count
            if counter_changed or slept > self._runtime.SLEEP_DETECT_THRESHOLD:
                detail = "kernel suspend counter changed" if counter_changed else f"~{slept:.0f}s clock gap"
                self._runtime._log(f"sleep detected ({detail})")
                try:
                    await self._handle_resume("sleep-detector")
                except Exception as e:
                    self._runtime._log(f"resume handling failed: {e}")

    def _ps_devices(self):
        """Open gamepad event nodes from udev links or sysfs names, without exclusive access."""
        candidates = {}
        for directory in ("/dev/input/by-id", "/dev/input/by-path"):
            try:
                for name in os.listdir(directory):
                    if name.endswith(("-event-joystick", "-event-gamepad")):
                        path = os.path.join(directory, name)
                        candidates[os.path.realpath(path)] = name
            except OSError:
                pass
        # Some SteamOS controllers have no by-id/by-path joystick link. Their event node still
        # appears in sysfs with a controller name, so include those devices as a fallback.
        self._ps_seen_inputs = []
        try:
            for name in os.listdir("/dev/input"):
                if not re.fullmatch(r"event\d+", name):
                    continue
                node = os.path.join("/dev/input", name)
                try:
                    with open(f"/sys/class/input/{name}/device/name", encoding="utf-8") as f:
                        device_name = f.read().strip()
                except OSError:
                    continue
                self._ps_seen_inputs.append(f"{name}:{device_name}")
                lower = device_name.lower()
                if any(token in lower for token in ("controller", "gamepad", "joystick", "dualsense",
                                                     "dualshock", "playstation", "xbox")):
                    candidates[os.path.realpath(node)] = device_name
        except OSError:
            pass
        wanted = set(candidates)
        for fd, path in list(self._ps_files.items()):
            if path not in wanted:
                try:
                    os.close(fd)
                except OSError:
                    pass
                del self._ps_files[fd]
                self._ps_labels.pop(fd, None)
        opened = {path for path in self._ps_files.values()}
        added = False
        for path in wanted - opened:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                self._ps_files[fd] = path
                self._ps_labels[fd] = candidates[path]
                added = True
            except OSError:
                continue
        return added

    def _ps_hid_devices(self):
        """Open Sony raw HID interfaces so Steam Input's evdev grab cannot hide the PS button."""
        candidates = {}
        try:
            names = os.listdir("/dev")
        except OSError:
            names = []
        for name in names:
            if not re.fullmatch(r"hidraw\d+", name):
                continue
            try:
                with open(f"/sys/class/hidraw/{name}/device/uevent", encoding="utf-8") as f:
                    fields = f.read().splitlines()
            except OSError:
                continue
            device_name = next((line.split("=", 1)[1] for line in fields
                                if line.startswith("HID_NAME=")), "")
            if "dualsense" in device_name.lower() or "dualshock" in device_name.lower():
                candidates[os.path.join("/dev", name)] = device_name
        wanted = {os.path.realpath(path): label for path, label in candidates.items()}
        for fd, path in list(self._ps_hid_files.items()):
            if path not in wanted:
                try:
                    os.close(fd)
                except OSError:
                    pass
                self._ps_hid_files.pop(fd, None)
                self._ps_hid_labels.pop(fd, None)
                self._ps_hid_state.pop(fd, None)
        opened = set(self._ps_hid_files.values())
        added = False
        for path in set(wanted) - opened:
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
                self._ps_hid_files[fd] = path
                self._ps_hid_labels[fd] = wanted[path]
                self._ps_hid_state[fd] = False
                added = True
            except OSError:
                continue
        return added

    @staticmethod
    def _dualsense_ps_pressed(report):
        """PS Home is bit 0 of buttons[2] in DualSense USB and Bluetooth reports."""
        if report and report[0] == 0x01 and len(report) >= 11:  # USB/simple input report
            return bool(report[10] & 0x01)
        if report and report[0] == 0x31 and len(report) >= 12:  # Bluetooth full input report
            return bool(report[11] & 0x01)
        return None

    async def _on_ps_press(self):
        """Return to the configured HDMI input after PS, but only while the TV is on another input."""
        if self._shutting_down or self._logind_sleeping:
            return
        if self._wake_cancelled.is_set():
            if self._sleep_recent():
                return
            # Steam can announce sleep and then abort before logind sees it. Old workers remain cancelled.
            self._wake_cancelled = threading.Event()
        cancelled = self._wake_cancelled
        now = time.monotonic()
        if now - self._ps_last_action < 3.0:
            return
        self._ps_last_action = now
        s = await asyncio.to_thread(self._runtime._load)
        if not self._runtime._paired(s):
            return
        target = int(s["hdmi_port"])
        try:
            current = await asyncio.to_thread(self._runtime._t_current_port, s, timeout=2.0)
            if cancelled.is_set() or current is None or current < 0 or current == target:
                return
            result = await asyncio.to_thread(self._runtime._tv_fast, s, cancelled)
            self._runtime._log(f"PS button: switch to HDMI {target}: {result}")
            self._runtime._record_result("wake", result)
        except Exception as e:
            self._runtime._log(f"PS button handling failed: {e}")

    async def _watch_ps_button(self):
        """Listen for PS only while the awake TV is on a known input other than the PC's HDMI."""
        event = struct.Struct("@llHHi")
        # Linux input event: EV_KEY=1; BTN_MODE=316 is the common PS/Guide mapping;
        # KEY_HOMEPAGE=172 covers controllers that expose PS as a keyboard-style key.
        watched_codes = {316, 172}
        away = False
        last_check = 0.0
        try:
            while True:
                if not self._ps_startup_done:
                    await asyncio.sleep(1.0)
                    continue
                now = time.monotonic()
                if now - last_check >= 3.0:
                    last_check = now
                    try:
                        s = await asyncio.to_thread(self._runtime._load)
                        active = self._runtime._paired(s)
                        current = (await asyncio.to_thread(self._runtime._t_current_port, s, timeout=2.0)
                                   if active else None)
                        new_away = (current is not None and current >= 0 and current != int(s["hdmi_port"]))
                        if new_away != away:
                            away = new_away
                            if away:
                                # Establish a baseline. A controller already connected when the TV is
                                # switched away must not cause an automatic input change.
                                self._ps_devices()
                                self._ps_hid_devices()
                                devices = ", ".join(sorted(set(self._ps_labels.values()) |
                                                           set(self._ps_hid_labels.values()))) or "none found"
                                inputs = ("; event devices: " + ", ".join(self._ps_seen_inputs)
                                          if not self._ps_labels else "")
                                self._runtime._log(f"PS listener: armed; TV is away from the PC HDMI; "
                                                   f"gamepads: {devices}{inputs}")
                            else:
                                self._runtime._log("PS listener: disarmed; TV is on the PC HDMI or unavailable")
                    except Exception as e:
                        away = False
                        self._runtime._log(f"PS listener: TV input check failed: {e}")
                if away:
                    new_evdev = self._ps_devices()
                    new_hidraw = self._ps_hid_devices()
                    if new_evdev or new_hidraw:
                        # A controller that reconnects after its power-saving timeout may not emit
                        # the PS key event that woke its Bluetooth link. Its new event node is the cue.
                        devices = ", ".join(sorted(set(self._ps_labels.values()) |
                                                   set(self._ps_hid_labels.values()))) or "unknown"
                        self._runtime._log(f"PS listener: gamepad event node appeared: {devices}")
                        await self._on_ps_press()
                else:
                    for fd in list(self._ps_files):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        self._ps_files.pop(fd, None)
                        self._ps_labels.pop(fd, None)
                    for fd in list(self._ps_hid_files):
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        self._ps_hid_files.pop(fd, None)
                        self._ps_hid_labels.pop(fd, None)
                        self._ps_hid_state.pop(fd, None)
                fds = list(self._ps_files)
                if fds:
                    for fd in fds:
                        try:
                            data = os.read(fd, event.size * 32)
                        except BlockingIOError:
                            continue
                        except OSError:
                            data = b""
                        if not data:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                            self._ps_files.pop(fd, None)
                            self._ps_labels.pop(fd, None)
                            continue
                        for offset in range(0, len(data) - event.size + 1, event.size):
                            _, _, typ, code, value = event.unpack_from(data, offset)
                            if typ == 1 and value == 1:
                                if self._ps_debug_remaining > 0:
                                    self._ps_debug_remaining -= 1
                                    self._runtime._log(f"PS listener: gamepad key event code={code} "
                                                       f"device={self._ps_labels.get(fd, 'unknown')}")
                                if code in watched_codes:
                                    self._runtime._log(f"PS listener: PS key detected (code={code})")
                                    await self._on_ps_press()
                for fd in list(self._ps_hid_files):
                    try:
                        report = os.read(fd, 256)
                    except BlockingIOError:
                        continue
                    except OSError:
                        report = b""
                    if not report:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        self._ps_hid_files.pop(fd, None)
                        self._ps_hid_labels.pop(fd, None)
                        self._ps_hid_state.pop(fd, None)
                        continue
                    pressed = self._dualsense_ps_pressed(report)
                    if pressed is None:
                        continue
                    previous = self._ps_hid_state.get(fd, False)
                    self._ps_hid_state[fd] = pressed
                    if pressed and not previous:
                        self._runtime._log(f"PS listener: PS HID button detected "
                                           f"device={self._ps_hid_labels.get(fd, 'DualSense')}")
                        await self._on_ps_press()
                await asyncio.sleep(0.1)
        finally:
            for fd in list(self._ps_files):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._ps_files.clear()
            self._ps_labels.clear()
            for fd in list(self._ps_hid_files):
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._ps_hid_files.clear()
            self._ps_hid_labels.clear()
            self._ps_hid_state.clear()

    # ----- lifecycle
    async def _startup(self):
        # No fixed delay: _tv_fast keeps retrying until the network and the TV answer (up to 45 s)
        try:
            r = await self._runtime._resume_all(self._wake_cancelled)
            self._runtime._log(f"startup: {r}")
            self._runtime._record_result("wake", r)
        finally:
            self._ps_startup_done = True

    async def _main(self):
        self._runtime._log(f"NetCEC loaded (root={os.geteuid() == 0})")
        self._ps_startup_done = False
        self._ps_last_action = 0.0
        self._ps_debug_remaining = 20
        self._last_suspends = self._runtime._suspend_count()
        self._runtime._log("resume duplicates are told apart by: " + ("the kernel suspend counter" if self._last_suspends is not None else "a 20 s timer"))
        loop = asyncio.get_event_loop()
        self._tasks = [loop.create_task(c) for c in
                       (self._startup(), self._watch_sleep(), self._logind_loop(), self._watch_ps_button())]
        await self._take_lock()

    async def _unload(self):
        # Everything here is synchronous on purpose: nothing may keep Decky waiting for the plugin
        self._runtime._stop.set()
        for t in list(self._tasks) + list(self._bg):
            t.cancel()
        self._runtime._kill_group(self._mon)
        self._runtime._adb_drop()
        await self._release_lock()
        self._runtime._log("NetCEC unloaded")
