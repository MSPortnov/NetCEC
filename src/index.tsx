import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import {
  ButtonItem,
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
  switch_input: boolean;
  suspend_action: string;
  shutdown_action: string;
  off_only_if_active: boolean;
  wake_rules: Record<string, boolean>;
  lang: string;
  lang_pref: string;
};
type Result = { ok: boolean; message: string; skipped?: boolean };
type UsbDev = { id: string; name: string; wake: boolean; bt: boolean };
type Info = { root: boolean; monitor: boolean; lock: boolean };

const getSettings = callable<[], Settings>("get_settings");
const setSettings = callable<[Partial<Settings>], void>("set_settings");
const getInfo = callable<[], Info>("get_info");
const pairStart = callable<[], Result>("pair_start");
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
    sec_tv: "TV (Sony Bravia)",
    tv_ip: "TV IP address",
    hint_tv: "On the TV: IP control > Authentication = Normal, enable Remote start. Then show the PIN and enter it below.",
    pin_show: "Show PIN on TV",
    not_paired: "Not paired",
    pin_label: "PIN shown on TV",
    pin_confirm: "Confirm PIN",
    hdmi_port: "HDMI port of this device",
    sec_todo: "TV: what to do",
    wake_tv: "Wake TV",
    switch_input: "Switch HDMI input",
    on_sleep: "When going to sleep",
    sl_none: "Do nothing",
    sl_off: "Turn TV off",
    sl_home: "Android TV home screen",
    unpair: "Unpair TV",
    unpair_confirm: "Press again to unpair",
    on_shutdown: "When shutting down",
    only_mine: "Only if TV is on my HDMI port",
    only_mine_d: "Don't switch off a TV someone is watching on another input",
    hook_on: "Sleep hook (systemd): active",
    hook_off: "Sleep hook (systemd): not running",
    lock_held: "lock held",
    sec_usb: "Wake sources (USB)",
    no_root: "Backend is not running as root, changes will fail.",
    no_usb: "No USB devices with wake support found.",
    bt_adapter: " · Bluetooth adapter",
    sec_lang: "Interface",
    lang_label: "Language",
    lang_auto: "Auto (system)",
  },
  ru: {
    loading: "Загрузка…",
    sec_tv: "ТВ (Sony Bravia)",
    tv_ip: "IP-адрес ТВ",
    hint_tv: "На ТВ: IP-управление → Аутентификация = Normal, включите «Remote start». Затем покажите PIN и введите его ниже.",
    pin_show: "Показать PIN на ТВ",
    not_paired: "Не сопряжено",
    pin_label: "PIN с экрана ТВ",
    pin_confirm: "Подтвердить PIN",
    hdmi_port: "HDMI-порт этого устройства",
    sec_todo: "ТВ: что делать",
    wake_tv: "Включать ТВ",
    switch_input: "Переключать HDMI-вход",
    on_sleep: "При уходе в сон",
    sl_none: "Ничего не делать",
    sl_off: "Выключать ТВ",
    sl_home: "Домашний экран Android TV",
    unpair: "Разорвать пару с ТВ",
    unpair_confirm: "Нажмите ещё раз для сброса",
    on_shutdown: "При выключении ПК",
    only_mine: "Только если ТВ на моём HDMI-порту",
    only_mine_d: "Не выключать ТВ, который кто-то смотрит на другом входе",
    hook_on: "Перехват сна (systemd): активен",
    hook_off: "Перехват сна (systemd): не работает",
    lock_held: "блокировка удерживается",
    sec_usb: "Источники пробуждения (USB)",
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
  return n === undefined ? v : v.replace("{n}", String(n));
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
  const [usb, setUsb] = useState<UsbDev[]>([]);
  const [info, setInfo] = useState<Info>({ root: true, monitor: false, lock: false });
  const [busy, setBusy] = useState(false);
  const [pin, setPin] = useState("");
  const [confirmUnpair, setConfirmUnpair] = useState(false);

  const lang: Lang = s ? resolveLang(s.lang_pref) : detectLang();
  const t = (k: string, n?: number) => tr(lang, k, n);

  const suspendOptions = useMemo(() => [
    { data: "none", label: t("sl_none") },
    { data: "off", label: t("sl_off") },
    { data: "home", label: t("sl_home") },
  ], [lang]);
  const shutdownOptions = useMemo(() => [
    { data: "none", label: t("sl_none") },
    { data: "off", label: t("sl_off") },
  ], [lang]);
  const langOptions = useMemo(() => [
    { data: "auto", label: t("lang_auto") },
    { data: "en", label: "English" },
    { data: "ru", label: "Русский" },
  ], [lang]);

  const refresh = () => getSettings().then(setS);
  const refreshStatus = () => tvStatus().then((r) => setStatus(r.message));
  const refreshInfo = () => getInfo().then(setInfo);

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
    })();
  }, []);

  useEffect(() => {
    restoreUntil = Date.now() + 2000; // panel (re)opened: go back to the remembered row
  }, []);

  useEffect(() => {
    // several tries: the dropdown menu closes and Steam moves the focus a moment after our re-render
    const ids = s ? [0, 150, 400, 900].map((ms) => window.setTimeout(restoreFocus, ms)) : [];
    return () => ids.forEach((id) => window.clearTimeout(id));
  }, [s, info, usb, busy, status]);

  if (!s) return <PanelSection title={TITLE}>{t("loading")}</PanelSection>;

  const update = (patch: Partial<Settings>) => {
    restoreUntil = Date.now() + 2000;
    setS({ ...s, ...patch });
    setSettings(patch).then(refreshInfo);
  };

  // Notifications only when something goes wrong
  const run = async (fn: () => Promise<Result>) => {
    setBusy(true);
    try {
      const r = await fn();
      if (!r.ok) toaster.toast({ title: TITLE, body: r.message });
    } finally {
      restoreUntil = Date.now() + 2000;
      setBusy(false);
      refresh();
      refreshStatus();
    }
  };

  const paired = s.paired;

  return (
    <>
      <PanelSection title={t("sec_tv")}>
        <PanelSectionRow>
          <div style={{ fontSize: 13 }}>{status}</div>
        </PanelSectionRow>
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
              <TextField
                label={t("tv_ip")}
                value={s.ip}
                onChange={(e) => update({ ip: e.target.value.trim() })}
              />
            </Row>
            <PanelSectionRow>
              <div style={{ fontSize: 12, opacity: 0.7 }}>{t("hint_tv")}</div>
            </PanelSectionRow>
            <Row id="pin-show">
              <ButtonItem
                layout="below"
                disabled={busy || !s.ip}
                description={t("not_paired")}
                onClick={() => run(() => pairStart())}
              >
                {t("pin_show")}
              </ButtonItem>
            </Row>
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
        <Row id="hdmi-port">
          <DropdownItem
            label={t("hdmi_port")}
            rgOptions={PORTS}
            selectedOption={s.hdmi_port}
            onChange={(o) => update({ hdmi_port: o.data as number })}
          />
        </Row>
      </PanelSection>

      <PanelSection title={t("sec_todo")}>
        <Row id="wake-tv">
          <ToggleField label={t("wake_tv")} checked={s.wake_tv} onChange={(v) => update({ wake_tv: v })} />
        </Row>
        <Row id="switch-input">
          <ToggleField
            label={t("switch_input")}
            checked={s.switch_input}
            onChange={(v) => update({ switch_input: v })}
          />
        </Row>
        <Row id="on-sleep">
          <DropdownItem
            label={t("on_sleep")}
            rgOptions={suspendOptions}
            selectedOption={s.suspend_action}
            onChange={(o) => update({ suspend_action: o.data as string })}
          />
        </Row>
        <Row id="on-shutdown">
          <DropdownItem
            label={t("on_shutdown")}
            rgOptions={shutdownOptions}
            selectedOption={s.shutdown_action}
            onChange={(o) => update({ shutdown_action: o.data as string })}
          />
        </Row>
        <Row id="only-mine">
          <ToggleField
            label={t("only_mine")}
            description={t("only_mine_d")}
            checked={s.off_only_if_active}
            onChange={(v) => update({ off_only_if_active: v })}
          />
        </Row>
        <PanelSectionRow>
          <div style={{ fontSize: 12, opacity: 0.7 }}>
            {(info.monitor ? t("hook_on") : t("hook_off")) + (info.lock ? " · " + t("lock_held") : "")}
          </div>
        </PanelSectionRow>
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

// Notifications only for problems, never for successful actions
const notify = (r: Result) => {
  if (!r.ok) toaster.toast({ title: TITLE, body: r.message });
};

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
  hook(sc?.System, "RegisterForOnResumeFromSuspend", async () => {
    const r = await onResume();
    if (!r.skipped) notify(r);
  });
  // results of actions the backend triggered itself (resume)
  const resumeListener = addEventListener<[Result]>("resume_result", (r) => {
    notify(r);
  });

  return {
    name: TITLE,
    titleView: <div className={staticClasses.Title}>{TITLE}</div>,
    content: <Content />,
    icon: <FaTv />,
    onDismount() {
      regs.forEach((r) => r?.unregister?.());
      removeEventListener("resume_result", resumeListener);
    },
  };
});
