import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { callable, definePlugin } from "@decky/api";
import {
  ButtonItem,
  Dropdown,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { FaTv } from "react-icons/fa";

type Lang = "en" | "ru";
type Settings = {
  ip: string;
  paired: boolean;
  hdmi_port: number;
  wake_tv: boolean;
  driver: string;
  adb_trained_ports: number[];
  journal: boolean;
  suspend_action: string;
  shutdown_action: string;
  wake_rules: Record<string, boolean>;
  lang: string;
  lang_pref: string;
};
type Result = { ok: boolean; message: string; skipped?: boolean };
type UsbDev = { id: string; name: string; wake: boolean; bt: boolean };
type Info = { root: boolean; monitor: boolean; lock: boolean };
type TvFound = { ip: string; name: string; model: string; kind: string };
type EventItem = { t: number; kind: string; ok: boolean; text: string };

const getSettings = callable<[], Settings>("get_settings");
const setSettings = callable<[Partial<Settings>], void>("set_settings");
const getInfo = callable<[], Info>("get_info");
const findTvs = callable<[], TvFound[]>("find_tvs");
const getEvents = callable<[], EventItem[]>("get_events");
const pairStart = callable<[], Result>("pair_start");
const trainHdmi = callable<[], Result>("train_hdmi");
const pairFinish = callable<[string], Result>("pair_finish");
const tvStatus = callable<[], Result>("tv_status");
const tvUnpair = callable<[], Result>("tv_unpair");
const usbList = callable<[], UsbDev[]>("usb_list");
const usbSet = callable<[string, boolean], UsbDev[]>("usb_set");
const onResume = callable<[], Result>("on_resume");
const onSuspend = callable<[string], Result>("on_suspend");

const TITLE = "NetCEC";

// ---------- localization ----------

const STR: Record<Lang, Record<string, string>> = {
  en: {
    loading: "Loading…",
    sec_tv: "TV",
    tv_ip: "TV IP address",
    find_tv: "Find TV on the network",
    searching: "Searching…",
    found_n: "Found: {n}",
    found_none: "Nothing found",
    found_none_hint: "Turn the TV on and check that it is on the same network, or enter its IP address below.",
    tv_list: "Found TVs",
    pick_tv: "Choose a TV",
    hint_tv: "Turn the TV on and press «Find TV» (or enter its IP address). On the TV: IP control > Authentication = Normal, enable Remote start. Then show the PIN and enter it below.",
    hint_tv_adb: "On the TV: Developer options > turn on Network debugging (USB debugging). Then press Connect: the TV asks to allow debugging, choose \u201cAlways allow\u201d.",
    adb_connect: "Connect",
    train_hdmi: "Teach the plugin to detect HDMI ports",
    train_hint: "The TV will visit HDMI 1–4, then return to the selected port.",
    trained_ports: "HDMI input detection has been learned.",
    training: "Teaching input detection…",
    kind_adb: "Android TV",
    pin_show: "Show PIN on TV",
    not_paired: "Not paired",
    pin_label: "PIN shown on TV",
    pin_confirm: "Confirm PIN",
    hdmi_port: "HDMI port of this device",
    sec_wake: "On start and wake-up",
    sec_sleep: "When going to sleep",
    sec_shutdown: "When shutting down",
    sec_events: "Journal",
    journal: "Action journal",
    ev_none: "Nothing has happened yet.",
    ev_wake: "Wake-up",
    ev_sleep: "Sleep",
    ev_shutdown: "Shutdown",
    need_pairing: "Pair the TV first (section above): until then the plugin does nothing.",
    opt_home: "To home screen",
    opt_off: "Turn TV off",
    hint_wake_on: "When SteamOS starts or wakes up, the TV is turned on (if it was off) and switched to HDMI {n} (if it is not on it yet).",
    hint_wake_off: "When SteamOS starts or wakes up, the TV is not touched.",
    hint_sl_home: "When SteamOS goes to sleep, the TV goes to its Android TV home screen if it is on HDMI {n} at that moment.",
    hint_sl_off: "When SteamOS goes to sleep, the TV is turned off if it is on HDMI {n} at that moment.",
    hint_sd_home: "When SteamOS shuts down, the TV goes to its Android TV home screen if it is on HDMI {n} at that moment.",
    hint_sd_off: "When SteamOS shuts down, the TV is turned off if it is on HDMI {n} at that moment.",
    wake_tv: "Wake TV and switch input",
    unpair: "Unpair TV",
    unpair_confirm: "Press again to unpair",
    hook_on: "Sleep hook (systemd): active",
    hook_off: "Sleep hook (systemd): not running",
    lock_held: "lock held",
    sec_usb: "Wake sources",
    no_root: "Backend is not running as root, changes will fail.",
    no_usb: "No USB devices with wake support found.",
    bt_adapter: " · Bluetooth adapter",
    sec_lang: "Interface",
    lang_label: "Language",
    lang_auto: "Auto (system)",
  },
  ru: {
    loading: "Загрузка…",
    sec_tv: "Телевизор",
    tv_ip: "IP-адрес ТВ",
    find_tv: "Найти ТВ в сети",
    searching: "Идёт поиск…",
    found_n: "Найдено: {n}",
    found_none: "Ничего не найдено",
    found_none_hint: "Включите ТВ и проверьте, что он в одной сети с приставкой, или введите его IP-адрес ниже.",
    tv_list: "Найденные ТВ",
    pick_tv: "Выберите ТВ",
    hint_tv: "Включите ТВ и нажмите «Найти ТВ» (или введите IP-адрес). На ТВ: IP-управление → Аутентификация = Normal, включите «Remote start». Затем покажите PIN и введите его ниже.",
    hint_tv_adb: "На ТВ: «Для разработчиков» → включите «Отладка по сети» (отладка по USB). Затем нажмите «Подключить»: ТВ спросит разрешение на отладку, выберите «Всегда разрешать».",
    adb_connect: "Подключить",
    train_hdmi: "Обучить плагин определять HDMI-порт?",
    train_hint: "ТВ по очереди переключится на HDMI 1–4, затем вернётся на выбранный порт.",
    trained_ports: "Определение HDMI-входов обучено.",
    training: "Обучаю определять входы…",
    kind_adb: "Android TV",
    pin_show: "Показать PIN на ТВ",
    not_paired: "Не сопряжено",
    pin_label: "PIN с экрана ТВ",
    pin_confirm: "Подтвердить PIN",
    hdmi_port: "HDMI-порт этого устройства",
    sec_wake: "При включении и пробуждении",
    sec_sleep: "При уходе в сон",
    sec_shutdown: "При выключении",
    sec_events: "Журнал",
    journal: "Журнал действий",
    ev_none: "Пока ничего не происходило.",
    ev_wake: "Пробуждение",
    ev_sleep: "Сон",
    ev_shutdown: "Выключение",
    need_pairing: "Сначала выполните сопряжение с ТВ (раздел выше): до этого плагин ничего не делает.",
    opt_home: "На домашний экран",
    opt_off: "Выключить ТВ",
    hint_wake_on: "При включении или пробуждении SteamOS включается ТВ (если он был выключен) и переключается на HDMI {n} (если он ещё не на нём).",
    hint_wake_off: "При включении и пробуждении SteamOS не трогаем ТВ.",
    hint_sl_home: "Когда SteamOS уходит в сон, ТВ переходит на домашний экран Android TV, если он сейчас на HDMI {n}.",
    hint_sl_off: "Когда SteamOS уходит в сон, ТВ выключается, если он сейчас на HDMI {n}.",
    hint_sd_home: "Когда SteamOS выключается, ТВ переходит на домашний экран Android TV, если он сейчас на HDMI {n}.",
    hint_sd_off: "Когда SteamOS выключается, ТВ выключается, если он сейчас на HDMI {n}.",
    wake_tv: "Включать ТВ и переключать вход",
    unpair: "Разорвать пару с ТВ",
    unpair_confirm: "Нажмите ещё раз для сброса",
    hook_on: "Перехват сна (systemd): активен",
    hook_off: "Перехват сна (systemd): не работает",
    lock_held: "блокировка удерживается",
    sec_usb: "Источники пробуждения",
    no_root: "Бэкенд запущен не от root, изменения не сработают.",
    no_usb: "USB-устройства с поддержкой пробуждения не найдены.",
    bt_adapter: " · Bluetooth-адаптер",
    sec_lang: "Интерфейс",
    lang_label: "Язык",
    lang_auto: "Авто (как в системе)",
  },
};

const detectLang = (): Lang => {
  const w = window as any;
  const raw = String(
    w.LocalizationManager?.m_rgLocalesToUse?.[0] ?? navigator.language ?? "en"
  ).toLowerCase();
  return raw.startsWith("ru") ? "ru" : "en";
};

const resolveLang = (pref: string): Lang =>
  pref === "en" || pref === "ru" ? pref : detectLang();

const tr = (lang: Lang, key: string, n?: number): string => {
  const v = STR[lang][key] ?? STR.en[key] ?? key;
  return n === undefined ? v : v.split("{n}").join(String(n));
};

// ---------- UI ----------

// ---------- remembered focus position ----------
// After a setting changes (or a dropdown closes) Steam moves the gamepad focus to the first
// focusable element of the plugin view (the "Back" button in the header). The id of the last
// focused row is kept at module level; for a short time after a user change a focus that is
// outside all rows is moved back there. Survives re-renders and closing/reopening the panel.
let lastFocusId = "";
let restoreUntil = 0;
const rowEls: Record<string, HTMLDivElement | null> = {};

function Row({ id, children }: { id: string; children: ReactNode }) {
  return (
    <PanelSectionRow>
      <div
        ref={(el) => {
          rowEls[id] = el;
        }}
        onFocus={() => {
          lastFocusId = id;
        }}
      >
        {children}
      </div>
    </PanelSectionRow>
  );
}

// A hint is a row of its own under the control (full width), not a description beside it.
const fmtTime = (epoch: number) => {
  const d = new Date(epoch * 1000);
  const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  if (d.toDateString() === new Date().toDateString()) return hm;
  return String(d.getDate()).padStart(2, "0") + "." + String(d.getMonth() + 1).padStart(2, "0") + " " + hm;
};

function Hint({ text }: { text: string }) {
  return (
    <PanelSectionRow>
      <div style={{ fontSize: 12, lineHeight: 1.35, opacity: 0.7 }}>{text}</div>
    </PanelSectionRow>
  );
}

const restoreFocus = () => {
  if (Date.now() > restoreUntil) return;
  const row = lastFocusId ? rowEls[lastFocusId] : null;
  if (!row) return;
  const active = row.ownerDocument.activeElement;
  if (active && row.contains(active)) return; // already on the remembered row
  const inRows = Object.values(rowEls).some((r) => !!r && !!active && r.contains(active));
  if (inRows) return; // the user moved to another row: leave it
  row.querySelector<HTMLElement>("[tabindex], button, input")?.focus();
};

const PORTS = [1, 2, 3, 4].map((n) => ({ data: n, label: `HDMI ${n}` }));

function Content() {
  const [s, setS] = useState<Settings | null>(null);
  const [status, setStatus] = useState("…");
  const [note, setNote] = useState("");  // why the last action of the user failed
  const [events, setEvents] = useState<EventItem[]>([]);
  const [usb, setUsb] = useState<UsbDev[]>([]);
  const [info, setInfo] = useState<Info>({ root: true, monitor: false, lock: false });
  const [busy, setBusy] = useState(false);
  const [pin, setPin] = useState("");
  const [confirmUnpair, setConfirmUnpair] = useState(false);
  const [found, setFound] = useState<TvFound[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);

  const lang: Lang = s ? resolveLang(s.lang_pref) : detectLang();
  const t = (k: string, n?: number) => tr(lang, k, n);

  const leaveOptions = useMemo(() => [
    { data: "home", label: t("opt_home") },
    { data: "off", label: t("opt_off") },
  ], [lang]);
  const tvOptions = useMemo(() => [
    { data: "", label: t("pick_tv") },
    ...found.map((f) => ({
      data: f.ip,
      label: `${f.name || "TV"}${f.model && f.model !== f.name ? " · " + f.model : ""}${f.kind === "adb" ? " · " + t("kind_adb") : ""} (${f.ip})`,
    })),
  ], [lang, found]);
  const langOptions = useMemo(() => [
    { data: "auto", label: t("lang_auto") },
    { data: "en", label: "English" },
    { data: "ru", label: "Русский" },
  ], [lang]);

  const refresh = () => getSettings().then(setS);
  const refreshStatus = () => tvStatus().then((r) => setStatus(r.message));
  const refreshEvents = () =>
    getEvents()
      .then((e) => setEvents((old) => (JSON.stringify(old) === JSON.stringify(e) ? old : e)))
      .catch(() => {});
  const refreshInfo = () => getInfo().then(setInfo);

  // Looks for supported TVs on the network; a single result is selected automatically.
  const discover = async (currentIp: string) => {
    setSearching(true);
    try {
      const list = await findTvs();
      setFound(list);
      if (list.length === 1 && !currentIp) {
        const selection = { ip: list[0].ip, driver: list[0].kind === "adb" ? "adb" : "sony" };
        await setSettings(selection);
        setS((cur) => (cur ? { ...cur, ...selection } : cur));
      }
    } catch (e) {
      setFound([]);
    } finally {
      setSearched(true);
      setSearching(false);
    }
  };

  useEffect(() => {
    (async () => {
      const st = await getSettings();
      const l = resolveLang(st.lang_pref);
      if (st.lang !== l) {
        await setSettings({ lang: l }); // backend messages follow the UI language
        st.lang = l;
      }
      setS(st);
      refreshStatus();
      refreshInfo();
      usbList().then(setUsb);
      if (!st.paired && !st.ip) discover(""); // first start: look for the TV right away
    })();
  }, []);

  useEffect(() => {
    restoreUntil = Date.now() + 2000; // panel (re)opened: go back to the remembered row
  }, []);

  useEffect(() => {
    // the journal of recent actions: look again every few seconds while the panel is open and the journal is on
    if (!s?.journal) {
      setEvents([]);
      return undefined;
    }
    refreshEvents();
    const id = window.setInterval(refreshEvents, 3000);
    return () => window.clearInterval(id);
  }, [s?.journal]);

  useEffect(() => {
    // several tries: the dropdown menu closes and Steam moves the focus a moment after our re-render
    const ids = s ? [0, 150, 400, 900].map((ms) => window.setTimeout(restoreFocus, ms)) : [];
    return () => ids.forEach((id) => window.clearTimeout(id));
  }, [s, info, usb, busy, status, found, searching]);

  if (!s) return <PanelSection title={TITLE}>{t("loading")}</PanelSection>;

  const update = (patch: Partial<Settings>) => {
    restoreUntil = Date.now() + 2000;
    setS({ ...s, ...patch });
    setSettings(patch).then(refreshInfo);
  };

  // The result of an action (pairing, a check) is shown in the panel; there are no pop-up notifications
  const run = async (fn: () => Promise<Result>) => {
    setBusy(true);
    try {
      const r = await fn();
      setNote(r.ok ? "" : r.message);
      refreshEvents();
    } finally {
      restoreUntil = Date.now() + 2000;
      setBusy(false);
      refresh();
      refreshStatus();
    }
  };

  const paired = s.paired;
  const adbTrainingComplete = [1, 2, 3, 4].every((port) => s.adb_trained_ports.includes(port));

  return (
    <>
      <PanelSection title={t("sec_tv")}>
        <PanelSectionRow>
          <div style={{ fontSize: 13 }}>{status}</div>
        </PanelSectionRow>
        {note && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, lineHeight: 1.35, color: "#e8b04a" }}>{note}</div>
          </PanelSectionRow>
        )}
        {paired ? (
          <Row id="top">
            <ButtonItem
              layout="below"
              disabled={busy}
              description={s.ip}
              onClick={() => {
                if (!confirmUnpair) {
                  setConfirmUnpair(true);
                  setTimeout(() => setConfirmUnpair(false), 4000);
                } else {
                  setConfirmUnpair(false);
                  run(tvUnpair);
                }
              }}
            >
              {confirmUnpair ? t("unpair_confirm") : t("unpair")}
            </ButtonItem>
          </Row>
        ) : (
          <>
            <Row id="top">
              <ButtonItem
                layout="below"
                disabled={busy || searching}
                description={
                  searching ? t("searching") : searched ? (found.length ? t("found_n", found.length) : t("found_none")) : undefined
                }
                onClick={() => discover(s.ip)}
              >
                {t("find_tv")}
              </ButtonItem>
            </Row>
            {searched && !searching && found.length === 0 && (
              <PanelSectionRow>
                <div style={{ fontSize: 12, opacity: 0.7 }}>{t("found_none_hint")}</div>
              </PanelSectionRow>
            )}
            {found.length > 0 && (
              <Row id="tv-list">
                <DropdownItem
                  label={t("tv_list")}
                  rgOptions={tvOptions}
                  selectedOption={found.some((f) => f.ip === s.ip) ? s.ip : ""}
                  onChange={(o) => {
                    const tv = found.find((f) => f.ip === o.data);
                    if (tv) update({ ip: tv.ip, driver: tv.kind === "adb" ? "adb" : "sony" });
                  }}
                />
              </Row>
            )}
            <Row id="ip">
              <TextField
                label={t("tv_ip")}
                value={s.ip}
                onChange={(e) => update({ ip: e.target.value.trim(), driver: "sony" })}
              />
            </Row>
            <PanelSectionRow>
              <div style={{ fontSize: 12, opacity: 0.7 }}>{t(s.driver === "adb" ? "hint_tv_adb" : "hint_tv")}</div>
            </PanelSectionRow>
            <Row id="pin-show">
              <ButtonItem
                layout="below"
                disabled={busy || !s.ip}
                description={t("not_paired")}
                onClick={() => run(() => pairStart())}
              >
                {t(s.driver === "adb" ? "adb_connect" : "pin_show")}
              </ButtonItem>
            </Row>
            {s.driver !== "adb" && (
              <>
                <Row id="pin-input">
                  <TextField
                    label={t("pin_label")}
                    value={pin}
                    onChange={(e) => setPin(e.target.value.trim())}
                  />
                </Row>
                <Row id="pin-confirm">
                  <ButtonItem
                    layout="below"
                    disabled={busy || !pin}
                    onClick={() =>
                      run(async () => {
                        const r = await pairFinish(pin);
                        if (r.ok) setPin("");
                        return r;
                      })
                    }
                  >
                    {t("pin_confirm")}
                  </ButtonItem>
                </Row>
              </>
            )}
          </>
        )}
        {s.paired && s.driver === "adb" && !adbTrainingComplete && (
          <Row id="train-hdmi">
            <ButtonItem
              layout="below"
              disabled={busy}
              description={busy ? t("training") : s.adb_trained_ports.length
                ? `${t("trained_ports")} HDMI ${s.adb_trained_ports.join(", ")}`
                : t("train_hint")}
              onClick={() => run(trainHdmi)}
            >
              {t("train_hdmi")}
            </ButtonItem>
          </Row>
        )}
        <Row id="hdmi-port">
          <DropdownItem
            label={t("hdmi_port")}
            rgOptions={PORTS}
            selectedOption={s.hdmi_port}
            onChange={(o) => update({ hdmi_port: o.data as number })}
          />
        </Row>
      </PanelSection>

      {!s.paired && (
        <PanelSection>
          <Hint text={t("need_pairing")} />
        </PanelSection>
      )}

      <div style={{ opacity: s.paired ? 1 : 0.4 }}>
        <PanelSection title={t("sec_wake")}>
          <Row id="wake-tv">
            <ToggleField
              label={t("wake_tv")}
              disabled={!s.paired}
              checked={s.wake_tv}
              onChange={(v) => update({ wake_tv: v })}
            />
          </Row>
          <Hint text={tr(lang, s.wake_tv ? "hint_wake_on" : "hint_wake_off", s.hdmi_port)} />
        </PanelSection>

        <PanelSection title={t("sec_sleep")}>
          <Row id="on-sleep">
            <Dropdown
              rgOptions={leaveOptions}
              disabled={!s.paired}
              selectedOption={s.suspend_action}
              onChange={(o) => update({ suspend_action: o.data as string })}
            />
          </Row>
          <Hint text={tr(lang, "hint_sl_" + s.suspend_action, s.hdmi_port)} />
          <PanelSectionRow>
            <div style={{ fontSize: 12, opacity: 0.7 }}>
              {(info.monitor ? t("hook_on") : t("hook_off")) + (info.lock ? " · " + t("lock_held") : "")}
            </div>
          </PanelSectionRow>
        </PanelSection>

        <PanelSection title={t("sec_shutdown")}>
          <Row id="on-shutdown">
            <Dropdown
              rgOptions={leaveOptions}
              disabled={!s.paired}
              selectedOption={s.shutdown_action}
              onChange={(o) => update({ shutdown_action: o.data as string })}
            />
          </Row>
          <Hint text={tr(lang, "hint_sd_" + s.shutdown_action, s.hdmi_port)} />
        </PanelSection>
      </div>

      <PanelSection title={t("sec_events")}>
        <Row id="journal">
          <ToggleField label={t("journal")} checked={s.journal} onChange={(v) => update({ journal: v })} />
        </Row>
        {s.journal && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, lineHeight: 1.4 }}>
              {events.length === 0 && <div style={{ opacity: 0.7 }}>{t("ev_none")}</div>}
              {events.map((e, i) => (
                <div key={i} style={{ marginBottom: 4 }}>
                  <span
                    style={{
                      display: "inline-block", width: 8, height: 8, borderRadius: 4, marginRight: 6,
                      background: e.ok ? "#4caf50" : "#e05050",
                    }}
                  />
                  <span style={{ opacity: 0.7 }}>{fmtTime(e.t)} </span>
                  <b>{t("ev_" + e.kind)}</b>: {e.text}
                </div>
              ))}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title={t("sec_usb")}>
        {!info.root && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, color: "#ff8a80" }}>{t("no_root")}</div>
          </PanelSectionRow>
        )}
        {usb.length === 0 && (
          <PanelSectionRow>
            <div style={{ fontSize: 12, opacity: 0.7 }}>{t("no_usb")}</div>
          </PanelSectionRow>
        )}
        {usb.map((d) => (
          <Row key={d.id} id={"usb-" + d.id}>
            <ToggleField
              label={d.name}
              description={d.id + (d.bt ? t("bt_adapter") : "")}
              checked={d.wake}
              onChange={async (v) => setUsb(await usbSet(d.id, v))}
            />
          </Row>
        ))}
      </PanelSection>

      <PanelSection title={t("sec_lang")}>
        <Row id="language">
          <DropdownItem
            label={t("lang_label")}
            rgOptions={langOptions}
            selectedOption={s.lang_pref}
            onChange={(o) =>
              update({ lang_pref: o.data as string, lang: resolveLang(o.data as string) })
            }
          />
        </Row>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // keep the backend's message language in sync with the Steam UI language
  getSettings()
    .then((st) => {
      const l = resolveLang(st.lang_pref);
      if (st.lang !== l) setSettings({ lang: l });
    })
    .catch(() => {});

  // Steam UI events. The sleep announcement comes BEFORE logind's, while the network is still up
  // (NetworkManager cuts it when logind signals). Which hooks exist depends on the Steam version,
  // so several are registered; the backend ignores duplicates.
  const sc = (window as any).SteamClient;
  const regs: any[] = [];
  const hook = (owner: any, fnName: string, cb: (...a: any[]) => void) => {
    const fn = owner?.[fnName];
    if (typeof fn !== "function") return;
    try {
      regs.push(fn.call(owner, cb));
    } catch (e) {
      // this hook is not available in this Steam version
    }
  };
  hook(sc?.System, "RegisterForOnSuspendRequest", () => {
    onSuspend("OnSuspendRequest");
  });
  hook(sc?.User, "RegisterForPrepareForSystemSuspendProgress", () => {
    onSuspend("PrepareForSystemSuspendProgress");
  });
  hook(sc?.System, "RegisterForOnResumeFromSuspend", () => {
    onResume();
  });

  return {
    name: TITLE,
    titleView: <div className={staticClasses.Title}>{TITLE}</div>,
    content: <Content />,
    icon: <FaTv />,
    onDismount() {
      regs.forEach((r) => r?.unregister?.());
    },
  };
});
