import asyncio
import base64
import functools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:  # loaded once here: importing inside a forked child can deadlock when other threads exist
    import ctypes
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except Exception:
    _LIBC = None

import decky

# The ADB client (Android TV) is optional: if it cannot be loaded, everything for Sony still works.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py_modules"))
import netcec_discovery
import netcec_usb
try:
    import netcec_adb
    _ADB_IMPORT_ERROR = ""
except Exception as _e:  # noqa: BLE001
    netcec_adb = None
    _ADB_IMPORT_ERROR = str(_e)

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")
USB_PATH = "/sys/bus/usb/devices"
_last_resume = 0.0
_last_renew = 0.0
_lang = "en"  # UI language for backend messages, refreshed on every _load()
SUSPEND_STATS = "/sys/power/suspend_stats"  # kernel counters of finished suspend attempts
MAX_EVENTS = 20       # the journal of recent actions keeps this many entries
HEAL_AFTER = 6.0      # the TV has not answered for this long: look for it at another address (s)
_events_lock = threading.Lock()
_stop = threading.Event()  # set on unload so worker threads end quickly


class AuthError(Exception):
    """TV rejected our credentials (pairing missing, expired or removed on the TV)."""


DEFAULTS = {
    # TV (Sony Bravia)
    "ip": "",
    "auth_cookie": "",         # set by PIN pairing
    "mac": "",                 # learned automatically, used for Wake-on-LAN
    "hdmi_port": 1,
    "wake_tv": True,           # on start/resume: wake the TV and switch it to our HDMI port
    "suspend_action": "off",   # on sleep: off | home (only if the TV shows our HDMI port)
    "shutdown_action": "off",  # on power-off: off | home (only if the TV shows our HDMI port; never on a reboot)
    "driver": "sony",          # which kind of TV: sony (IP control) | adb (Android TV over ADB)
    "adb_ok": False,           # the TV has accepted our ADB key (set by the backend only)
    "adb_inputs": {},          # HDMI port -> input id learned on this Android TV
    "settings_version": 8,
    # USB wake sources: {"vid:pid": true|false}
    "wake_rules": {},
    "journal": False,          # keep (and show) the journal of recent actions; off: nothing is recorded
    "lang": "en",              # resolved UI language, written by the frontend
    "lang_pref": "auto",       # auto | en | ru
}


PRIVATE = ("auth_cookie", "mac", "adb_ok", "adb_inputs")  # never sent to / accepted from the UI

LEAVE_ACTIONS = ("home", "off")  # what to do with the TV on sleep / shutdown (always one of them)
_settings_lock = threading.RLock()  # every read-modify-write of the settings file holds it
_USB_ID_RE = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}")


def _host_ok(h):
    """IPv4 address or plain host name: nothing that could change the URL (/, @, ?, #, spaces)."""
    if not isinstance(h, str) or not h or len(h) > 253:
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", h):
        return all(int(p) <= 255 for p in h.split("."))
    label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    return re.fullmatch(label + r"(?:\." + label + r")*", h) is not None


def _is_bool(v):
    return isinstance(v, bool)


# What each setting may contain. A half-typed IP address is allowed (only characters are checked);
# the strict check is _host_ok() right before the TV is contacted.
_VALID = {
    "ip": lambda v: isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9.\-]{0,253}", v) is not None,
    "auth_cookie": lambda v: isinstance(v, str) and "\r" not in v and "\n" not in v and len(v) < 2000,
    "mac": lambda v: isinstance(v, str) and (v == "" or re.fullmatch(r"[0-9A-Fa-f]{2}([:-][0-9A-Fa-f]{2}){5}", v) is not None),
    "hdmi_port": lambda v: isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 8,
    "wake_tv": _is_bool,
    "suspend_action": lambda v: v in LEAVE_ACTIONS,
    "shutdown_action": lambda v: v in LEAVE_ACTIONS,
    "driver": lambda v: v in ("sony", "adb"),
    "adb_ok": _is_bool,
    "adb_inputs": lambda v: isinstance(v, dict),
    "settings_version": lambda v: v == 8,
    "wake_rules": lambda v: isinstance(v, dict),
    "journal": _is_bool,
    "lang": lambda v: v in ("en", "ru"),
    "lang_pref": lambda v: v in ("auto", "en", "ru"),
}


def _sanitize(raw) -> dict:
    """All keys present, every invalid value replaced by its default."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key, default in DEFAULTS.items():
        v = raw.get(key, default)
        if key == "wake_rules":
            v = ({k: x for k, x in v.items() if isinstance(k, str) and _USB_ID_RE.fullmatch(k) and isinstance(x, bool)}
                 if isinstance(v, dict) else {})
        elif key == "adb_inputs":
            v = ({k: x for k, x in v.items() if isinstance(k, str) and k in {"1", "2", "3", "4"}
                  and isinstance(x, str) and re.fullmatch(
                      r"(?:HW[A-Za-z0-9_.-]{1,64}|URI:[A-Za-z0-9%._:/+\-]{1,240})", x)}
                 if isinstance(v, dict) else {})
        elif not _VALID[key](v):
            v = default
        out[key] = v
    return out


def _migrate(data):
    """Settings written by older versions are converted step by step.
    Before 1.3.8 (no settings_version): "wake the TV" and "switch the input" were two switches; they are one now
    ("wake the TV" also switches the input).
    Before 1.3.9 (version below 3): sleep and shutdown offered "do nothing" and a separate "only if the TV is on
    my HDMI port" switch; "do nothing" became "turn off".
    Before 1.3.10 (version below 4): "turn off if on my port" is the only way now: both actions (home screen /
    turn off) act only if the TV shows our HDMI port, so that choice became plain "turn off"."""
    if not data:
        return data
    version = data.get("settings_version", 1)
    if version < 2:
        if data.get("switch_input") and not data.get("wake_tv"):
            data["wake_tv"] = True
        if data.get("shutdown_action") == "off" and data.get("off_only_if_active", True):
            data["shutdown_action"] = "off_if_mine"
    if version < 3:
        mine = data.get("off_only_if_active", True)
        if data.get("suspend_action", "none") in ("none", "off"):
            data["suspend_action"] = "off" if data.get("suspend_action") == "off" and not mine else "off_if_mine"
        if data.get("shutdown_action", "none") == "none":
            data["shutdown_action"] = "off_if_mine"
    if version < 4:  # "if the TV is on my HDMI port" is part of every action now: nobody else's TV is touched
        for key in ("suspend_action", "shutdown_action"):
            if data.get(key) == "off_if_mine":
                data[key] = "off"
    # version 5 added ADB pairing; version 6 added learned input IDs; version 7 restored Sony's
    # API driver after the earlier temporary ADB test mode. Version 8 removes that Sony test mode;
    # make sure an existing Sony API pairing uses the Sony driver.
    if version < 8 and data.get("auth_cookie"):
        data.update(driver="sony", adb_ok=False, adb_inputs={})
    data["settings_version"] = 8
    return data


def _read_file(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("settings file is not a JSON object")
    return data


def _load() -> dict:
    global _lang
    with _settings_lock:
        try:
            data = _read_file(SETTINGS_FILE)
        except FileNotFoundError:
            data = {}
        except Exception as e:
            # Never overwrite a damaged file silently: it would wipe the pairing. Use the last good
            # copy and keep the damaged file next to it for inspection.
            _log(f"settings file unreadable ({e}); trying the backup")
            try:
                data = _read_file(SETTINGS_FILE + ".bak")
                restored = True
            except Exception:
                data, restored = {}, False
            try:
                os.replace(SETTINGS_FILE, SETTINGS_FILE + ".corrupt")
            except OSError:
                pass
            if restored:
                _save(data)
                _log("settings restored from the backup")
        s = _sanitize(_migrate(data))
    _lang = s["lang"]
    return s


def _save(settings: dict) -> None:
    """Atomic: write a temporary file, keep the last good version as .bak, then rename over the
    real file. The file holds the pairing key, so it is private to its owner (0600)."""
    clean = _sanitize(settings)
    with _settings_lock:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:  # only a readable file may become the backup
            _read_file(SETTINGS_FILE)
            shutil.copyfile(SETTINGS_FILE, SETTINGS_FILE + ".bak")
            os.chmod(SETTINGS_FILE + ".bak", 0o600)
        except Exception:
            pass
        os.replace(tmp, SETTINGS_FILE)


def _update(change) -> dict:
    """Read-modify-write under one lock: concurrent changes (UI, pairing renewal, worker threads)
    can no longer overwrite each other. `change(settings)` edits the dict in place."""
    with _settings_lock:
        s = _load()
        change(s)
        _save(s)
        return s


def _suspend_count():
    """How many suspend attempts have finished (successful + failed), as counted by the kernel; None if the
    kernel does not tell. Every attempt adds exactly one, so the three "the device woke up" sources can be
    told apart from a second real wake-up without any timer."""
    try:
        total = 0
        for name in ("success", "fail"):
            with open(f"{SUSPEND_STATS}/{name}") as f:
                total += int(f.read().strip())
        return total
    except Exception:
        return None


def _events_file():
    return os.path.join(os.path.dirname(SETTINGS_FILE), "events.json")


def _read_events():
    try:
        with open(_events_file(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _clear_events():
    with _events_lock:
        try:
            os.remove(_events_file())
        except OSError:
            pass


def _record(kind, ok, text):
    """Journal of what the plugin did (shown in the UI, newest first). Does nothing while the journal is
    switched off. Never raises."""
    try:
        if not _load()["journal"]:
            return
        with _events_lock:
            events = _read_events()
            events.insert(0, {"t": int(time.time()), "kind": kind, "ok": bool(ok), "text": str(text)[:200]})
            del events[MAX_EVENTS:]
            tmp = _events_file() + ".tmp"
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False)
            os.replace(tmp, _events_file())
    except Exception as e:
        _log(f"could not write the events journal: {e}")


def _record_result(kind, res):
    """Journal entry for the result of an action; nothing for "nothing to do" and for quiet results."""
    if res and not res.get("quiet") and not res.get("empty"):
        _record(kind, res["ok"], res.get("plain") or res["message"])


def _log(msg):
    try:
        decky.logger.info(msg)
    except Exception:
        pass


def _clocks():
    """(boottime, monotonic). BOOTTIME keeps counting during suspend, MONOTONIC does not."""
    return time.clock_gettime(time.CLOCK_BOOTTIME), time.monotonic()


def _ok(msg):
    return {"ok": True, "message": msg}


def _fail(msg):
    return {"ok": False, "message": msg}


TR = {
    "en": {
        "need_ip": "Set the TV IP address first",
        "need_auth": "Pair with the TV first (Show PIN on TV)",
        "need_pair": "Pairing lost or expired. Pair with the TV again (PIN)",
        "not_paired": "Not paired",
        "no_wake": "TV did not wake (check Remote start / network standby)",
        "switched": "Switched to HDMI {port}",
        "adb_approve": "Confirm on the TV: \u201cAllow USB debugging?\u201d, choose \u201cAlways allow\u201d, then press Connect again.",
        "adb_unavailable": "Android TV support is not available: {err}",
        "adb_train_ok": "HDMI detection learned for: {ports}",
        "adb_train_partial": "Learned: {ports}. Could not identify: {missing}",
        "adb_train_none": "The TV did not expose a stable active HDMI identifier. No mapping was saved.",
        "adb_train_restore_failed": "HDMI IDs were learned, but the TV did not confirm a return to HDMI {port}.",
        "sony_api_required": "Sony Bravia uses Sony IP control here. Enable IP control and pair with the PIN.",
        "what_undetectable": "input could not be determined",
        "switched_in": "Switched to HDMI {port} in {secs} s",
        "ip_changed": "TV found at its new address {ip}",
        "switch_failed": "Input switch failed: {err}",
        "sent_unconfirmed": "HDMI {port}: switch command sent, the TV did not confirm it",
        "what_unknown": "unknown input",
        "what_other": "another input",
        "left_on": "TV left on ({what})",
        "turning_off": "TV is turning off",
        "off_failed": "Power off failed: {err}",
        "not_configured": "Not configured",
        "tv_http": "TV error: HTTP {code}",
        "no_conn": "No connection: {err}",
        "tv_state": "TV: {st}",
        "st_standby": "standby",
        "st_activating": "starting",
        "tv_on": "TV: on · {where}",
        "where_unknown": "input unknown",
        "where_other": "other input/app",
        "pin_enter": "Enter the PIN shown on the TV",
        "paired": "Paired",
        "pair_unexpected": "Unexpected reply ({code}). On the TV set IP control authentication to Normal",
        "pair_need_pin": "Enter the TV IP and the PIN",
        "pair_wrong": "Wrong PIN. Press Show PIN again",
        "pair_failed": "Pairing failed (HTTP {code})",
        "wake_failed": "Wake settings not applied (root needed?)",
        "part_tv": "TV",
        "part_wake": "Wake sources",
        "error": "error",
        "nothing": "Nothing to do",
        "dup": "Skipped (duplicate event)",
        "reboot_left": "Reboot: TV left on",
        "home_sent": "TV sent to the Android TV home screen",
        "unpaired": "Pairing removed. Set up the TV again",
    },
    "ru": {
        "need_ip": "Сначала укажите IP-адрес телевизора",
        "need_auth": "Сначала выполните сопряжение с ТВ (кнопка «Показать PIN на ТВ»)",
        "need_pair": "Сопряжение потеряно или истекло. Выполните сопряжение с ТВ заново (PIN)",
        "not_paired": "Не сопряжено",
        "no_wake": "ТВ не включился (проверьте «Remote start» и сетевой режим ожидания)",
        "switched": "Переключено на HDMI {port}",
        "adb_approve": "Подтвердите на ТВ запрос \u00abРазрешить отладку\u00bb (выберите \u00abВсегда разрешать\u00bb) и нажмите \u00abПодключить\u00bb ещё раз.",
        "adb_unavailable": "Поддержка Android TV недоступна: {err}",
        "adb_train_ok": "Определение HDMI обучено для портов: {ports}",
        "adb_train_partial": "Обучено: {ports}. Не удалось определить: {missing}",
        "adb_train_none": "Телевизор не выдал устойчивый идентификатор активного HDMI. Таблица не сохранена.",
        "adb_train_restore_failed": "Входы обучены, но ТВ не подтвердил возврат на HDMI {port}.",
        "sony_api_required": "Sony Bravia здесь работает через Sony IP API. Включите IP-управление и выполните сопряжение по PIN.",
        "what_undetectable": "вход определить не удалось",
        "switched_in": "Переключено на HDMI {port} за {secs} с",
        "ip_changed": "ТВ найден по новому адресу {ip}",
        "switch_failed": "Не удалось переключить вход: {err}",
        "sent_unconfirmed": "HDMI {port}: команда отправлена, ТВ не подтвердил переключение",
        "what_unknown": "вход неизвестен",
        "what_other": "другой вход",
        "left_on": "ТВ оставлен включённым ({what})",
        "turning_off": "ТВ выключается",
        "off_failed": "Не удалось выключить: {err}",
        "not_configured": "Не настроено",
        "tv_http": "Ошибка ТВ: HTTP {code}",
        "no_conn": "Нет связи: {err}",
        "tv_state": "ТВ: {st}",
        "st_standby": "в режиме ожидания",
        "st_activating": "запускается",
        "tv_on": "ТВ: включён · {where}",
        "where_unknown": "вход неизвестен",
        "where_other": "другой вход/приложение",
        "pin_enter": "Введите PIN-код, показанный на ТВ",
        "paired": "Сопряжено",
        "pair_unexpected": "Неожиданный ответ ({code}). В настройках ТВ включите аутентификацию IP-управления «Normal»",
        "pair_need_pin": "Введите IP ТВ и PIN",
        "pair_wrong": "Неверный PIN. Нажмите «Показать PIN на ТВ» ещё раз",
        "pair_failed": "Не удалось выполнить сопряжение (HTTP {code})",
        "wake_failed": "Настройки пробуждения не применены (нужен root?)",
        "part_tv": "ТВ",
        "part_wake": "Пробуждение",
        "error": "ошибка",
        "nothing": "Нечего делать",
        "dup": "Пропущено (повторное событие)",
        "reboot_left": "Перезагрузка: ТВ оставлен включённым",
        "home_sent": "ТВ переведён на домашний экран Android TV",
        "unpaired": "Сопряжение сброшено. Настройте ТВ заново",
    },
}


def _t(key, **kw):
    return (TR.get(_lang, TR["en"]).get(key) or TR["en"][key]).format(**kw)


# ---------- subprocess helpers (Decky's bundled env breaks system binaries) ----------

def _env():
    env = dict(os.environ)
    orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
    env.pop("LD_LIBRARY_PATH", None)
    if orig is not None:
        env["LD_LIBRARY_PATH"] = orig
    return env


def _exe(args):
    """Resolve the binary (Decky's PATH may lack /usr/bin) and return the full argv."""
    path = _env().get("PATH", "") + ":/usr/bin:/usr/sbin:/bin:/sbin"
    return [shutil.which(args[0], path=path) or args[0]] + list(args[1:])


def _run(args, timeout=6):
    try:
        p = subprocess.run(_exe(args), capture_output=True, text=True, timeout=timeout, env=_env())
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


# ---------- Sony Bravia ----------

def _auth_headers(s):
    h = {"Content-Type": "application/json"}
    if s["auth_cookie"]:
        h["Cookie"] = s["auth_cookie"]
    return h


def _rest(s, service, method, params=None, timeout=4):
    body = json.dumps({"method": method, "id": 1, "params": params or [], "version": "1.0"}).encode()
    req = urllib.request.Request(f"http://{s['ip']}/sony/{service}", body, _auth_headers(s))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(str(e))
        raise
    if "error" in res:
        if res["error"] and res["error"][0] in (401, 403):
            raise AuthError(str(res["error"]))
        raise RuntimeError(res["error"])
    return res


def _look(s, timeout=4):
    """(port, what): which HDMI port the TV shows (0 = something else, None = no answer at all) and a
    short text for the log. An error ANSWER (the TV is on its home screen or screensaver and plays
    nothing) means the TV is reachable, so the port is 0, not None: otherwise a TV that is on would be
    treated as switched off."""
    try:
        uri = _rest(s, "avContent", "getPlayingContentInfo", timeout=timeout)["result"][0].get("uri", "")
    except AuthError:
        raise
    except RuntimeError as e:  # raised by _rest for an error response of the TV
        return 0, f"error {e}"[:60]
    except Exception as e:  # no connection, timeout, malformed answer
        return None, f"no answer ({type(e).__name__})"
    m = re.match(r"extInput:hdmi\?port=(\d+)", uri)
    return (int(m.group(1)), f"hdmi {m.group(1)}") if m else (0, f"other {uri[:50]}")


def _current_port(s, timeout=4):
    """HDMI port the TV currently shows; 0 = something else; None = the TV did not answer."""
    return _look(s, timeout)[0]


def _guess_mac(ip):
    try:
        with open("/proc/net/arp") as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if p[0] == ip and p[3] != "00:00:00:00:00:00":
                    return p[3]
    except Exception:
        pass
    return ""


def _wol(s):
    mac = (s.get("mac") or _guess_mac(s["ip"])).replace(":", "").replace("-", "")
    if len(mac) != 12:
        return
    packet = b"\xff" * 6 + bytes.fromhex(mac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, ("255.255.255.255", 9))


def _learn_mac(s):
    if not s.get("mac"):
        mac = _guess_mac(s["ip"])
        if mac:
            _update(lambda cur: cur.update(mac=mac))
            s["mac"] = mac


REG_BODY = {
    "method": "actRegister", "id": 8, "version": "1.0",
    "params": [
        {"clientid": "netcec:steamos", "nickname": "NetCEC", "level": "private"},
        [{"value": "yes", "function": "WOL"}],
    ],
}


def _register(s, pin=None, cookie=None):
    """actRegister. Without PIN/cookie the TV answers 401 and shows a PIN on screen."""
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if pin is not None:
        headers["Authorization"] = "Basic " + base64.b64encode((":" + pin).encode()).decode()
    req = urllib.request.Request(
        f"http://{s['ip']}/sony/accessControl", json.dumps(REG_BODY).encode(), headers
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            cookies = r.headers.get_all("Set-Cookie") or []
            return 200, next((c.split(";")[0] for c in cookies if c.startswith("auth=")), "")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _store_cookie(s, cookie):
    _update(lambda cur: cur.update(auth_cookie=cookie))
    s["auth_cookie"] = cookie
    _learn_mac(s)  # the TV has just answered, so its MAC (for Wake-on-LAN) is in the ARP table


def _renew(s, force=False):
    """Re-register with the stored cookie (what a remote app does) to keep the pairing alive.
    Returns False only if the TV demands a new PIN. If the TV is unreachable nothing is done."""
    global _last_renew
    if not s["auth_cookie"]:
        return False
    if not force and time.monotonic() - _last_renew < 600:
        return True
    try:
        code, cookie = _register(s, None, s["auth_cookie"])
    except Exception:
        return True
    if code == 200:
        if cookie:
            _store_cookie(s, cookie)
        _last_renew = time.monotonic()
        return True
    return False


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except AuthError:
            return _fail(_t("need_pair"))
    return wrapper


def _pair_start(s):
    if not _host_ok(s["ip"]):
        return _fail(_t("need_ip"))
    if _is_adb(s):
        return _adb_pair(s)
    try:
        code, cookie = _register(s)
    except Exception as e:
        if _port_open(s["ip"], ADB_PORT):  # no Sony API, but ADB answers: an Android TV
            try:
                if any(tv["ip"] == s["ip"] and tv["kind"] == "sony" for tv in _discover(timeout=1.0)):
                    return _fail(_t("sony_api_required"))
            except Exception:
                pass
            s["driver"] = "adb"
            result = _adb_pair(s)
            try:
                adb_brand = _adb_brand(s) if result.get("ok") else "other"
            except Exception:
                adb_brand = "other"
            if result.get("ok") and adb_brand == "sony":
                _update(lambda cur: cur.update(driver="sony", adb_ok=False, adb_inputs={}))
                s.update(driver="sony", adb_ok=False, adb_inputs={})
                return _fail(_t("sony_api_required"))
            return result
        return _fail(_t("no_conn", err=e))
    if code == 401:
        return _ok(_t("pin_enter"))
    if code == 200 and cookie:
        _store_cookie(s, cookie)
        return _ok(_t("paired"))
    return _fail(_t("pair_unexpected", code=code))


def _pair_finish(s, pin):
    pin = (pin or "").strip()
    if not _host_ok(s["ip"]) or not pin:
        return _fail(_t("pair_need_pin"))
    try:
        code, cookie = _register(s, pin)
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    if code == 200 and cookie:
        _store_cookie(s, cookie)
        return _ok(_t("paired"))
    if code in (401, 403):
        return _fail(_t("pair_wrong"))
    return _fail(_t("pair_failed", code=code))


def _paired(s):
    """A TV is set up: Sony needs the pairing key, an Android TV needs the accepted ADB key."""
    return bool(s["ip"] and (s["adb_ok"] if s["driver"] == "adb" else s["auth_cookie"]))


def _tv_check(s):
    if not _host_ok(s["ip"]):
        return _fail(_t("need_ip"))
    if not _paired(s):
        return _fail(_t("need_auth"))
    return None


@_guard
def _status(s):
    if not s["ip"]:
        return _fail(_t("not_configured"))
    if not _paired(s):
        return _fail(_t("not_paired"))
    if _is_adb(s):
        port, what = _adb_look(s, 3.0)
        if port is None:
            if what.startswith("asleep"):
                return _ok(_t("tv_state", st=_t("st_standby")))
            return _fail(_t("no_conn", err=what))
        where = _t("where_unknown") if port < 0 else (_t("where_other") if port == 0 else f"HDMI {port}")
        return _ok(_t("tv_on", where=where))
    try:
        st = _rest(s, "system", "getPowerStatus")["result"][0]["status"]
    except urllib.error.HTTPError as e:
        return _fail(_t("tv_http", code=e.code))
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    if st != "active":
        return _ok(_t("tv_state", st=_t("st_" + st) if st in ("standby", "activating") else st))
    if not _renew(s):
        raise AuthError("pairing expired")
    port = _current_port(s)  # authenticated call: also validates the pairing
    where = _t("where_unknown") if port is None else (_t("where_other") if port == 0 else f"HDMI {port}")
    return _ok(_t("tv_on", where=where))


IRCC_HOME = "AAAAAQAAAAEAAABgAw=="  # remote control "Home" key (Sony's standard IRCC codes)
IRCC_BACK = "AAAAAgAAAJcAAAAjAw=="  # "Back" (Return): ends a screensaver


def _ircc(s, code, timeout=3):
    body = (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        '<u:X_SendIRCC xmlns:u="urn:schemas-sony-com:service:IRCC:1"><IRCCCode>%s</IRCCCode>'
        "</u:X_SendIRCC></s:Body></s:Envelope>" % code
    ).encode()
    headers = {"Content-Type": "text/xml; charset=UTF-8",
               "SOAPACTION": '"urn:schemas-sony-com:service:IRCC:1#X_SendIRCC"'}
    if s["auth_cookie"]:
        headers["Cookie"] = s["auth_cookie"]
    req = urllib.request.Request(f"http://{s['ip']}/sony/IRCC", body, headers)
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthError(str(e))
        raise


def _rediscover_ip(s):
    """The TV got another address from the router? Look for a Sony TV on the network whose MAC address is the
    one we paired with. A TV that is off does not answer the search, so nothing changes then."""
    mac = (s.get("mac") or "").lower().replace("-", ":")
    if not mac:
        return None
    for tv in _discover(timeout=2.5):
        if tv["ip"] != s["ip"] and _guess_mac(tv["ip"]).lower().replace("-", ":") == mac:
            return tv["ip"]
    return None


LEAVE_LOOK_TIMEOUTS = (2.0, 1.5)  # the TV is sometimes slow to answer: the question is asked twice at most


@_guard
def _leave(s, mode):
    """Fast path for sleep/shutdown (logind waits about 5 s): few round trips, short timeouts.
    mode: 'off' = power off, 'home' = Android TV home screen.
    Only if the TV shows our HDMI port: a TV somebody else is watching is never touched."""
    bad = _tv_check(s)
    if bad:
        return bad
    t0 = time.monotonic()
    cur = None
    for timeout in LEAVE_LOOK_TIMEOUTS:
        cur = _t_current_port(s, timeout=timeout)
        if cur is not None:  # an answer, even "something else"; only silence is asked again
            break
    _log(f"leave: input check {time.monotonic() - t0:.2f}s -> {cur}")
    if cur != int(s["hdmi_port"]):
        if cur is None:
            label = _t("what_unknown")
        elif cur == 0:
            label = _t("what_other")
        elif cur < 0:
            label = _t("what_undetectable")  # an Android TV that is on, whose input cannot be told: not touched
        else:
            label = f"HDMI {cur}"
        return _ok(_t("left_on", what=label))
    try:
        if mode == "home":
            _t_home(s)
            msg = _t("home_sent")
        else:
            _t_off(s)
            msg = _t("turning_off")
    except AuthError:
        raise
    except Exception as e:
        return _fail(_t("off_failed", err=e))
    _log(f"leave {mode}: done in {time.monotonic() - t0:.2f}s")
    return _ok(msg)


def _power_state(s):
    """'active' | 'standby' | other status | None (unreachable)."""
    try:
        return _rest(s, "system", "getPowerStatus", timeout=2)["result"][0]["status"]
    except Exception:
        return None


# ---------- Android TV over ADB ----------

ADB_PORT = 5555
ADB_APPROVAL_WAIT = 25.0  # pairing waits this long for the user to accept our key on the TV (s)
ADB_IDLE = 30.0           # a connection that was not used for this long is not reused (s)

# How to open an HDMI port, per brand. The ids are those of the TV's own inputs (they differ between TVs).
ANDROID_TV_INPUTS = {
    "tcl": {
        "uri": "content://android.media.tv/passthrough/com.tcl.tvinput%2F.passthroughinput.TvPassThroughService%2F{hw}",
        "hw": {1: "HW1413744128", 2: "HW1413744384", 3: "HW1413744640", 4: "HW1413745664"},
    },
}
GENERIC_HDMI_KEYCODES = {1: 243, 2: 244, 3: 245, 4: 246}  # KEYCODE_TV_INPUT_HDMI_1..4: a guess for other brands

_adb_lock = threading.RLock()
_adb_state = {"ip": None, "conn": None, "used": 0.0, "key": None, "brand": {}}

# one round trip: power state, the last HDMI input that was opened, the activity in front
_ADB_LOOK_CMD = ("dumpsys power | sed -n '/mWakefulness=/p' | sed -n '1p'; echo '##starter'; "
                 "dumpsys activity starter | grep -E 'mIntent=|dat=content://|HW[0-9]'; "
                 "echo '##resumed'; dumpsys activity activities | grep -E 'topResumedActivity|mResumedActivity'")


def _adb_key():
    if _adb_state["key"] is None:
        _adb_state["key"] = netcec_adb.load_or_create_key(os.path.join(os.path.dirname(SETTINGS_FILE), "adbkey.json"))
    return _adb_state["key"]


def _adb_drop():
    """Close the cached connection (never waits: it is also called while the plugin unloads)."""
    conn, _adb_state["conn"] = _adb_state["conn"], None
    if conn:
        conn.close()


def _adb_connection(s):
    st = _adb_state
    if st["conn"] and st["ip"] == s["ip"] and st["conn"].alive and time.monotonic() - st["used"] < ADB_IDLE:
        return st["conn"], True
    _adb_drop()
    conn = netcec_adb.Connection(s["ip"], _adb_key(), port=ADB_PORT, timeout=3.0).connect()
    st.update(ip=s["ip"], conn=conn)
    return conn, False


def _adb_shell(s, command, timeout=4.0):
    """Run a shell command on the Android TV. One connection is reused for a short time; if a reused connection
    turns out to be dead, it is opened again once."""
    if netcec_adb is None:
        raise RuntimeError(_t("adb_unavailable", err=_ADB_IMPORT_ERROR))
    with _adb_lock:
        for attempt in (1, 2):
            conn, reused = _adb_connection(s)
            try:
                out = conn.shell(command, timeout)
                _adb_state["used"] = time.monotonic()
                return out
            except Exception:
                _adb_drop()
                if not reused or attempt == 2:
                    raise


def _adb_brand(s):
    brands = _adb_state["brand"]
    if s["ip"] not in brands:
        out = _adb_shell(s, "getprop ro.product.manufacturer; getprop ro.product.brand", 3.0).lower()
        brands[s["ip"]] = "sony" if "sony" in out else ("tcl" if "tcl" in out else "other")
    return brands[s["ip"]]


def _adb_parse_look(text, hw_ports, learned_ports=None):
    """(port, what) from the output of _ADB_LOOK_CMD: the port the TV shows, 0 = something else, -1 = the TV is on
    but the input cannot be told, None = the TV is off."""
    power = re.search(r"mWakefulness=(\w+)", text)
    if not power:
        return -1, "no power state in the answer"
    if power.group(1) in ("Asleep", "Dozing"):
        return None, f"asleep ({power.group(1)})"
    parts = text.split("##starter", 1)[-1].split("##resumed", 1)
    starter, resumed = parts[0], (parts[1] if len(parts) > 1 else "")
    shown = " ".join(resumed.split())[:80]
    source_ids = _adb_input_tokens(starter)
    on_input = any(word in resumed.lower() for word in ("tvinput", "passthrough", "hdmi", "com.sony.dtv.tvx"))
    if not on_input:
        return 0, f"awake, not on an input ({shown or 'no activity'})"
    learned_ports = learned_ports or {}
    learned_source_ports = {source: int(port) for port, source in learned_ports.items()}
    for hw in re.findall(r"HW\d+", starter):  # the most recently opened input first
        if hw in hw_ports:
            return hw_ports[hw], f"hdmi {hw_ports[hw]} ({hw})"
    for source in _adb_input_tokens(starter):
        if source in learned_source_ports:
            port = learned_source_ports[source]
            return port, f"hdmi {port} (learned source)"
    detail = f", source {source_ids[0][:48]}" if len(source_ids) == 1 else ""
    return -1, f"on an input, which one is unknown{detail} ({shown})"


def _adb_look(s, timeout=4, log=True):
    try:
        brand = _adb_brand(s)
        out = _adb_shell(s, _ADB_LOOK_CMD, max(timeout, 3.0))
    except Exception as e:
        return None, f"no answer ({type(e).__name__})"
    hw_ports = {hw: port for port, hw in ANDROID_TV_INPUTS.get(brand, {}).get("hw", {}).items()}
    learned_ports = s.get("adb_inputs", {})
    port, what = _adb_parse_look(out, hw_ports, learned_ports)
    if log:
        _log(f"adb look: {what} | {' '.join(out.split())[:1000]}")
    return port, what


def _adb_source_id(text):
    """Stable current input ID, only when a TV input activity is in front."""
    parts = text.split("##starter", 1)[-1].split("##resumed", 1)
    starter, resumed = parts[0], (parts[1] if len(parts) > 1 else "")
    lower = resumed.lower()
    if not any(word in lower for word in ("tvinput", "passthrough", "hdmi", "com.sony.dtv.tvx")):
        return None
    ids = _adb_input_tokens(starter)
    return ids[0] if len(ids) == 1 else None


def _adb_input_tokens(starter):
    """Stable IDs exposed by Android's current input intent; prefer the full TIF URI."""
    uris = list(dict.fromkeys(re.findall(
        r"content://android\.media\.tv/passthrough/[^\s}\"']+", starter, re.IGNORECASE)))
    # Some TVs print the same intent twice, with path separators encoded in one copy and
    # decoded in the other (Sony: HW5 appears as both %2F...%2FHW5 and /.../HW5).
    # Normalize first; genuinely different source IDs remain ambiguous and are rejected.
    normalized = list(dict.fromkeys(urllib.parse.unquote(uri) for uri in uris))
    if len(normalized) == 1:
        return ["URI:" + normalized[0]]
    if len(normalized) > 1:
        return []
    return list(dict.fromkeys(re.findall(r"\bHW\d+\b|\bHDMI[_-]?[1-9]\d*\b", starter, re.IGNORECASE)))


def _adb_read_source_id(s):
    out = _adb_shell(s, _ADB_LOOK_CMD, 4.0)
    return _adb_source_id(out)


def _adb_train_inputs(s):
    """Visit HDMI 1–4, learn stable per-port source IDs, then restore the configured port."""
    if not _is_adb(s) or not _paired(s):
        return _fail(_t("not_paired"))
    try:
        if _adb_power_state(s) != "active":
            return _fail(_t("adb_train_none"))
        _update(lambda cur: cur.update(adb_inputs={}))  # discard stale IDs before this calibration
        s["adb_inputs"] = {}
        _t_home(s)  # start from a known non-HDMI screen so a stale source ID cannot be learned
        time.sleep(0.5)
        if _adb_read_source_id(s) is not None:
            return _fail(_t("adb_train_none"))
        learned, previous = {}, None
        for port in range(1, 5):
            try:
                _adb_switch(s, port)
            except Exception as e:
                _log(f"ADB HDMI training: HDMI {port} command failed: {e}")
                continue
            started, candidate, stable = time.monotonic(), None, 0
            while time.monotonic() - started < 5.0 and not _stop.is_set():
                time.sleep(0.35)
                source = _adb_read_source_id(s)
                if source and source == candidate:
                    stable += 1
                elif source:
                    candidate, stable = source, 1
                else:
                    candidate, stable = None, 0
                if stable >= 2:
                    break
            if stable < 2 or not candidate:
                _log(f"ADB HDMI training: HDMI {port} has no stable source ID")
                continue
            if candidate == previous and port != int(s["hdmi_port"]):
                _log(f"ADB HDMI training: HDMI {port} did not change source ({candidate})")
                continue
            if candidate in learned.values():
                old_port = next(k for k, value in learned.items() if value == candidate)
                learned.pop(old_port, None)
                _log(f"ADB HDMI training: duplicate source {candidate} for HDMI {old_port}/{port}; ignored")
                continue
            learned[port] = candidate
            previous = candidate
            _log(f"ADB HDMI training: HDMI {port} -> {candidate}")

        if learned:
            mapping = {str(k): v for k, v in learned.items()}
            _update(lambda cur: cur.update(adb_inputs=mapping))
            s["adb_inputs"] = mapping

        target = int(s["hdmi_port"])
        restored = False
        try:
            _adb_switch(s, target)
            expected = learned.get(target)
            if expected:
                started, candidate, stable = time.monotonic(), None, 0
                while time.monotonic() - started < 5.0 and not _stop.is_set():
                    time.sleep(0.35)
                    source = _adb_read_source_id(s)
                    if source == expected:
                        stable += 1
                        if stable >= 2:
                            restored = True
                            break
                    else:
                        stable = 0
        except Exception as e:
            _log(f"ADB HDMI training: could not restore HDMI {target}: {e}")
        found = ", ".join(f"HDMI {p}" for p in sorted(learned))
        missing = ", ".join(f"HDMI {p}" for p in range(1, 5) if p not in learned)
        if not learned:
            return _fail(_t("adb_train_none"))
        if target in learned and not restored:
            return _fail(_t("adb_train_restore_failed", port=target))
        if missing:
            return _fail(_t("adb_train_partial", ports=found, missing=missing))
        return _ok(_t("adb_train_ok", ports=found))
    except Exception as e:
        _log(f"ADB HDMI training failed: {type(e).__name__}: {e}")
        return _fail(_t("adb_train_none"))


def _adb_power_state(s):
    out = _adb_shell(s, "dumpsys power | grep -m1 mWakefulness=", SEND_TIMEOUT + 1)
    m = re.search(r"mWakefulness=(\w+)", out)
    if not m:
        raise RuntimeError("no power state")
    return "standby" if m.group(1) in ("Asleep", "Dozing") else "active"


def _adb_switch_command(s, port):
    profile = ANDROID_TV_INPUTS.get(_adb_brand(s))
    if profile and port in profile["hw"]:
        uri = profile["uri"].format(hw=profile["hw"][port])
        return f'am start -a android.intent.action.VIEW -d "{uri}"'
    code = GENERIC_HDMI_KEYCODES.get(port)
    if code is None:
        raise RuntimeError(f"HDMI {port} is not supported on this TV")
    return f"input keyevent {code}"


def _adb_go(s, port, press_back):
    """Switch to an HDMI port; with press_back the Back key (it ends a screensaver) goes at the same moment: both
    run on the TV at once instead of one after the other."""
    command = _adb_switch_command(s, port)
    if press_back:
        command = f"input keyevent 4 & {command}; wait"
    out = _adb_shell(s, command, 5.0)
    if "Error" in out or "Exception" in out:
        raise RuntimeError(" ".join(out.split())[:120])


def _adb_switch(s, port):
    _adb_go(s, port, False)


def _port_open(ip, port, timeout=0.8):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _adb_pair(s):
    """Connect to an Android TV and wait until the user accepts our key on its screen."""
    if netcec_adb is None:
        return _fail(_t("adb_unavailable", err=_ADB_IMPORT_ERROR))
    try:
        with _adb_lock:
            _adb_drop()
            netcec_adb.Connection(s["ip"], _adb_key(), port=ADB_PORT, timeout=4.0).connect(
                approval_wait=ADB_APPROVAL_WAIT).close()
    except netcec_adb.NeedsApproval:
        return _fail(_t("adb_approve"))
    except Exception as e:
        return _fail(_t("no_conn", err=e))
    _update(lambda cur: cur.update(driver="adb", adb_ok=True, adb_inputs={}))
    s["driver"], s["adb_ok"], s["adb_inputs"] = "adb", True, {}
    _learn_mac(s)
    return _ok(_t("paired"))


# ---------- the TV, whatever kind it is: what _tv_fast and _leave ask of a TV ----------

def _is_adb(s):
    return s.get("driver") == "adb"


def _t_power_state(s):
    return _adb_power_state(s) if _is_adb(s) else _power_state(s)


def _t_look(s, timeout):
    return _adb_look(s, timeout) if _is_adb(s) else _look(s, timeout=timeout)


def _t_current_port(s, timeout=4):
    return _adb_look(s, timeout, log=False)[0] if _is_adb(s) else _current_port(s, timeout=timeout)


def _t_back(s):
    """The key that ends a screensaver (pressed before the input switch)."""
    if _is_adb(s):
        _adb_shell(s, "input keyevent 4", SEND_TIMEOUT + 1)
    else:
        _ircc(s, IRCC_BACK, timeout=SEND_TIMEOUT)


def _t_switch(s, port):
    if _is_adb(s):
        _adb_switch(s, port)
    else:
        _rest(s, "avContent", "setPlayContent", [{"uri": f"extInput:hdmi?port={port}"}], timeout=SEND_TIMEOUT)


def _t_go(s, port, press_back):
    """The key that ends a screensaver and the input switch, sent at the same moment: the TV starts leaving the
    screensaver and switching together instead of one after the other."""
    if _is_adb(s):
        _adb_go(s, port, press_back)
        return
    key = None
    if press_back:
        def back():
            try:
                _t_back(s)
            except Exception:  # a key that did not go through is not worth failing the switch
                pass
        key = threading.Thread(target=back, daemon=True)
        key.start()
    try:
        _t_switch(s, port)
    finally:
        if key:
            key.join(SEND_TIMEOUT + 1)


def _t_wake(s):
    """Power on (and Wake-on-LAN): neither may fail the caller."""
    try:
        if _is_adb(s):
            _adb_shell(s, "input keyevent 224", SEND_TIMEOUT + 1)
        else:
            _rest(s, "system", "setPowerStatus", [{"status": True}], timeout=SEND_TIMEOUT)
    except Exception:
        pass
    try:
        _wol(s)
    except Exception:
        pass


def _t_home(s):
    if _is_adb(s):
        _adb_shell(s, "input keyevent 3", 3.0)
    else:
        _ircc(s, IRCC_HOME, timeout=2)


def _t_off(s):
    if _is_adb(s):
        _adb_shell(s, "input keyevent 223", 3.0)
    else:
        _rest(s, "system", "setPowerStatus", [{"status": False}], timeout=2)


def _t_verifies(s):
    """Only report an input as confirmed when its current ID is known to match that HDMI port."""
    if not _is_adb(s):
        return True
    try:
        brand = _adb_state["brand"].get(s.get("ip"), "other")
        return brand in ANDROID_TV_INPUTS or str(int(s["hdmi_port"])) in s.get("adb_inputs", {})
    except Exception:
        return False


SEND_TIMEOUT = 2      # seconds for every request to the TV while waking / switching
CONFIRM_STEP = 0.4    # after the switch the TV is asked this often which input it shows (s) ...
CONFIRM_WINDOW = 8.0  # ... for this long: after a resume a TV reports the input only after 5-9 s (until then: "Illegal State")
SWITCH_SENDS = 2      # the switch command is sent at most this many times (never more while the TV is busy)
RETRY_PAUSE = 0.25    # pause between attempts while the TV or the network is not answering yet (s)
WAKE_WINDOW = 45      # how long a sleeping TV may take to wake up (s)


def _tv_fast(s):
    """Wake the TV and switch it to our HDMI port as early as possible.
    Nothing is asked first and nothing is waited for: the remote's Back key (it ends a running screensaver,
    which a plain input switch does not) and the input switch go out at the same moment. Then the TV is
    asked every CONFIRM_STEP until it reports the port: it needs seconds for that, and sending the command again
    while it is still busy only restarts its switching. Only if it has not confirmed after
    CONFIRM_WINDOW the command is sent one more time; after that the result is "sent, not confirmed".
    If the TV is off, it is woken (power on + Wake-on-LAN) every ~3 s and polled until it is
    active, for up to WAKE_WINDOW seconds."""
    bad = _tv_check(s)
    if bad:
        return bad
    port = int(s["hdmi_port"])
    t0 = time.monotonic()
    deadline = t0 + WAKE_WINDOW
    renewed, n, sends, last, first, healing_tried = False, 0, 0, "", True, False
    steps, saw, answered = [], [], []  # for the log: step durations, what the TV said, when it first answered

    def look():
        t1 = time.monotonic()
        cur, what = _t_look(s, SEND_TIMEOUT)
        if cur is not None and not answered:
            answered.append(time.monotonic() - t0)
        if len(saw) < 16:
            saw.append(f"{what} {time.monotonic() - t1:.2f}")
        return cur

    def timed(label, fn, *args, **kwargs):
        t1 = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            steps.append(f"{label} {time.monotonic() - t1:.2f}")

    def summary():
        reach = f"TV answered after {answered[0]:.1f} s; " if answered else ""
        return f"{reach}steps: {', '.join(steps)}; TV said: {' | '.join(saw)}"

    while time.monotonic() < deadline and not _stop.is_set():
        st = None
        try:
            # First try: no state check, the command goes at once (a Sony TV in standby refuses it and the wake path
            # starts). An Android TV in standby would swallow it silently, so its power state is asked first.
            if not first or _is_adb(s):
                st = _t_power_state(s)
            if (first and not _is_adb(s)) or st == "active":
                timed("go", _t_go, s, port, sends == 0)  # Back (only the first time) and the switch together
                if not answered:
                    answered.append(time.monotonic() - t0)  # the TV took the command: it is up
                sends += 1
                _learn_mac(s)
                if not _t_verifies(s):  # the command went through and that is all this kind of TV can tell
                    _log(f"TV: HDMI {port} command sent, not yet verified, {time.monotonic() - t0:.1f} s ({summary()})")
                    return _ok(_t("sent_unconfirmed", port=port))
                t1, confirmed = time.monotonic(), False
                while time.monotonic() - t1 < CONFIRM_WINDOW and not _stop.is_set():
                    time.sleep(CONFIRM_STEP)
                    if look() == port:
                        confirmed = True
                        break
                steps.append(f"wait {time.monotonic() - t1:.2f}")
                if confirmed:
                    _log(f"TV: HDMI {port} confirmed after {time.monotonic() - t0:.1f} s, "
                         f"{sends} switch command(s) ({summary()})")
                    return _ok(_t("switched_in", port=port, secs=f"{time.monotonic() - t0:.1f}"))
                if sends >= SWITCH_SENDS:  # the command was accepted, the TV just never said so
                    _log(f"TV: HDMI {port} command sent {sends}x, not confirmed ({summary()})")
                    return _ok(_t("sent_unconfirmed", port=port))
                first = False
                continue  # nothing happened for a whole window: send the command once more
        except AuthError:
            if renewed or not _renew(s, force=True):
                raise
            renewed = True
            continue
        except Exception as e:
            last = str(e)
        first = False
        if not answered and not healing_tried and time.monotonic() - t0 >= HEAL_AFTER:
            healing_tried = True  # once per run: a TV that is simply off does not answer the search either
            new_ip = _rediscover_ip(s)
            if new_ip:
                s["ip"] = new_ip
                _update(lambda cur: cur.update(ip=new_ip))
                _record("wake", True, _t("ip_changed", ip=new_ip))
                _log(f"TV: found at the new address {new_ip}")
                first = True  # try the switch at once
                continue
        if st != "active" and n % 12 == 0:
            _t_wake(s)
        n += 1
        time.sleep(RETRY_PAUSE)
    _log(f"TV: gave up after {time.monotonic() - t0:.1f} s ({summary()})")
    return _fail(_t("switch_failed", err=last)) if answered else _fail(_t("no_wake"))


@_guard
def _tv_actions(s):
    """Wake/switch according to settings. None = nothing configured."""
    if not s["ip"] or not s["wake_tv"]:
        return None
    return _tv_fast(s)


@_guard
def _tv_suspend(s):
    if not s["ip"]:
        return None
    return _leave(s, s["suspend_action"])


def _is_reboot():
    rc, out = _run(["systemctl", "list-jobs", "--no-legend", "--no-pager"], timeout=2)
    return bool(re.search(r"(reboot|kexec)\.target", out))


@_guard
def _tv_shutdown(s, typ=None):
    if not s["ip"]:
        return None
    reboot = (typ in ("reboot", "kexec", "soft-reboot")) if typ else _is_reboot()
    if reboot:  # the TV is only turned off on a real power-off, never on a reboot
        return {**_ok(_t("reboot_left")), "quiet": True}
    return _leave(s, s["shutdown_action"])


# ---------- finding the TV on the network (SSDP / UPnP) ----------

SSDP_ADDR = ("239.255.255.250", 1900)
SSDP_TARGETS = (
    "urn:schemas-sony-com:service:ScalarWebAPI:1",
    "urn:schemas-sony-com:service:IRCC:1",
    "upnp:rootdevice",
)


def _ssdp_search(timeout):
    """Send SSDP M-SEARCH requests and collect {ip: [description URLs]} of everything that answers.
    A TV answers with several descriptions (Chromecast, UPnP renderer, ...), so all are kept."""
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.bind(("", 0))
        for _ in range(2):  # UDP may lose a packet
            for st in SSDP_TARGETS:
                msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                       'MAN: "ssdp:discover"\r\nMX: 2\r\nST: %s\r\n\r\n' % st).encode()
                try:
                    sock.sendto(msg, SSDP_ADDR)
                except OSError as e:
                    _log(f"SSDP send failed: {e}")
        end = time.monotonic() + timeout
        while not _stop.is_set():
            left = end - time.monotonic()
            if left <= 0:
                break
            sock.settimeout(left)
            try:
                data, (ip, _port) = sock.recvfrom(4096)
            except OSError:  # timeout or socket error: stop listening
                break
            headers = {}
            for line in data.decode("utf-8", "replace").split("\r\n")[1:]:
                key, _, value = line.partition(":")
                if value:
                    headers[key.strip().lower()] = value.strip()
            location = headers.get("location")
            if not location or location in found.get(ip, []) or len(found.get(ip, [])) >= 4:
                continue  # the same device answers several requests with the same description
            found.setdefault(ip, []).append(location)
            _log(f"SSDP answer from {ip}: st={headers.get('st', '?')} location={location} "
                 f"server={headers.get('server', '?')}")
    return found


def _fetch_description(ip, location):
    """Download the UPnP device description. Only from the device that answered (never follow other hosts)."""
    u = urllib.parse.urlparse(location)
    if u.scheme != "http" or u.hostname != ip:
        _log(f"SSDP: {ip} gave a description URL that is not on its own address ({location}), ignored")
        return None
    try:
        with urllib.request.urlopen(location, timeout=2) as r:
            return r.read(262144).decode("utf-8", "replace")
    except Exception as e:
        _log(f"SSDP: description of {ip} not available: {e}")
        return None


def _unescape(text):
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&#39;", "'"), ("&amp;", "&")):
        text = text.replace(a, b)
    return text


def _xml_text(xml_text, name):
    """First <name>…</name> value; namespace prefixes are ignored. (xml.etree is not available in
    Decky's bundled Python, so the small UPnP description is read with a regular expression.)"""
    m = re.search(r"<(?:[\w.-]+:)?%s>\s*([^<]*?)\s*</(?:[\w.-]+:)?%s>" % (name, name), xml_text)
    return _unescape(m.group(1)) if m else ""


def _parse_tv_description(xml_text):
    """(name, model) if this UPnP/Cast description belongs to a Sony Bravia: Sony as the manufacturer
    and either the Scalar Web API (UPnP renderer description) or "BRAVIA" in the name (Chromecast
    description, which is the only one some Google TV models answer with)."""
    if "sony" not in _xml_text(xml_text, "manufacturer").lower():
        return None
    name, model = _xml_text(xml_text, "friendlyName"), _xml_text(xml_text, "modelName")
    if "ScalarWebAPI" not in xml_text and "bravia" not in (name + " " + model).lower():
        return None
    return name, model


def _discover(timeout=3.0):
    """TVs on the local network: Sony Bravias and Android TVs with network debugging on.
    [{"ip", "name", "model", "kind"}], kind is "sony" or "adb"."""
    return netcec_discovery.discover(
        timeout, _ssdp_search, _fetch_description, _parse_tv_description, _xml_text,
        _port_open, netcec_adb is not None, ADB_PORT, _log,
    )


# ---------- sleep / shutdown helpers ----------

SLEEP_POLL = 0.5     # resume detector tick (s)
SLEEP_DETECT_THRESHOLD = 8.0  # clock-only fallback when the kernel suspend counter is unavailable
SLEEP_BUDGET = 4.0   # logind waits at most 5 s for our lock
LOCK_ARGS = [
    "systemd-inhibit", "--what=sleep:shutdown", "--who=NetCEC",
    "--why=Turn the TV off before sleep or shutdown", "--mode=delay", "sleep", "infinity",
]
MONITOR_ARGS = ["dbus-monitor", "--system", "type='signal',interface='org.freedesktop.login1.Manager'"]


def _kill_group(p):
    """Kill a child and everything it started: systemd-inhibit runs `sleep infinity` as its own
    child, and killing only systemd-inhibit would leave that `sleep` running forever."""
    if not p or p.returncode is not None:
        return
    try:
        os.killpg(p.pid, 9)  # the child leads its own session/group (start_new_session=True)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def _pdeathsig():
    """Run in the child: get SIGKILL when the plugin process dies (no orphaned lock or monitor)."""
    try:
        if _LIBC:
            _LIBC.prctl(1, 9)  # PR_SET_PDEATHSIG, SIGKILL
    except Exception:
        pass


# ---------- USB wake sources ----------

def _rd(path):
    return netcec_usb.read(path)


def _usb_devices():
    return netcec_usb.devices(USB_PATH, _rd)


def _usb_set(dev_id, enabled):
    return netcec_usb.set_device(dev_id, enabled, _usb_devices, _rd)


def _apply_wake_rules(s):
    """Re-apply the saved USB wake rules. Returns a result only if nothing could be applied."""
    rules = s.get("wake_rules") or {}
    if not rules:
        return None
    n = sum(_usb_set(k, bool(v)) for k, v in rules.items())
    return None if n else _fail(_t("wake_failed"))


def _public_usb():
    return [{k: v for k, v in d.items() if k != "paths"} for d in _usb_devices()]


# ---------- orchestration ----------

async def _resume_all():
    s = _load()
    tasks = {}
    tasks["TV"] = asyncio.to_thread(_tv_actions, s)
    tasks["Wake"] = asyncio.to_thread(_apply_wake_rules, s)
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    parts, plain, ok = [], [], True
    for name, r in zip(tasks, results):
        if isinstance(r, Exception):
            parts.append(f"{_t('part_' + name.lower())}: {_t('error')} {r}")
            plain.append(f"{_t('error')} {r}")
            ok = False
        elif r is not None:
            parts.append(f"{_t('part_' + name.lower())}: {r['message']}")
            plain.append(r["message"])
            ok = ok and r["ok"]
    res = {"ok": ok, "message": "; ".join(parts) if parts else _t("nothing"),
           "plain": "; ".join(plain), "empty": not parts}
    _log(f"actions done: {res}")
    return res


from netcec_plugin import Plugin
Plugin.bind(globals())
