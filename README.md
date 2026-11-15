# NetCEC

**English** · [Русский](README.ru.md)

**NetCEC turns your Sony Bravia TV on and off together with your SteamOS box.**
When the box wakes up, the TV switches on and jumps to the right HDMI input. When the box goes to sleep or
shuts down, the TV turns off (or goes back to its home screen). Everything works over your home network,
so you don't need HDMI-CEC. It is a plugin for [Decky Loader](https://decky.xyz/) and is used in Game Mode.

## What it can do

- **Wake the TV and switch to your HDMI input** when the box starts or wakes up from sleep.
- **Turn the TV off, or send it to the Android TV home screen,** when the box goes to sleep.
- **Turn the TV off when the box shuts down.** A reboot never turns the TV off.
- **Leave the TV alone if someone is watching something else.** With "Only if TV is on my HDMI port"
  turned on, the TV is only switched off when it is showing your box.
- **Choose which USB devices can wake the box,** for example the Bluetooth adapter yes, the mouse no.
- **English and Russian** interface (follows the Steam language, or pick it manually).

## What you need

- A SteamOS device with Decky Loader installed.
- A Sony Bravia TV on Android TV, on the same network as the box.

## Setting up

1. **On the TV:** open Settings > Network > Home network setup > IP control. Set *Authentication* to
   **Normal** and turn on **Remote start**.
2. **In NetCEC:** type the TV's IP address (you can find it in the TV's network settings).
3. Press **Show PIN on TV**. A code appears on the TV screen.
4. Type that code into **PIN shown on TV** and press **Confirm PIN**. The status line shows *Paired*.
5. Pick the **HDMI port** the box is plugged into, then choose what the TV should do on wake, sleep and shutdown.

The pairing renews itself every time it is used. If it gets lost (for example you removed the device on the
TV), the status will ask you to pair again.

## Installing

You need Node.js and pnpm on the box (one time: `nvm install 20 && npm i -g pnpm@9`). Then, in the plugin folder:

    bash update.sh

The script builds the plugin, copies it to `~/homebrew/plugins/NetCEC/` and restarts Decky.

Prefer to build on another computer (Mac or Linux) and install over SSH? Run
`bash deploy.sh <box IP address>` there instead. Node.js is downloaded automatically if it is missing (pnpm is fetched on the fly),
the SSH user defaults to `deck` (change it with `DECK_USER=name`), and `bash deploy.sh <IP> logs`
saves diagnostic logs to `~/Downloads`.

The TV also reacts when the plugin itself starts, so restarting Decky switches the TV on.

## If something doesn't work

- **The TV does not turn on.** Check that *Remote start* is enabled on the TV, and that the TV keeps its
  network connection in standby (a "network standby" or similar option in the TV settings).
- **The status says "Pairing lost or expired".** Press *Unpair TV* and pair again (steps 2 to 4).
- **The TV does not turn off on sleep.** Make sure "When going to sleep" is not set to "Do nothing", and that
  the "Sleep hook" line in the plugin says *active*.
- **The TV stays on when the box sleeps.** If "Only if TV is on my HDMI port" is on, the TV is left alone
  when it shows a different input.
- **Logs:** `~/homebrew/logs/NetCEC/`.

## For developers

- Backend: `main.py` (Python, runs as root: it writes USB wake settings and takes a systemd lock).
  Frontend: `src/index.tsx` (React, built with `pnpm build` into `dist/`).
- TV control: Sony Bravia IP Control (REST + IRCC), PIN pairing, Wake-on-LAN as a backup wake-up.
- On start and on resume the plugin sends the wake / input-switch commands right away (2 s timeouts),
  checks which input the TV shows, and stops as soon as it is on the right port. The input switch is
  sent at most 3 times; a sleeping TV is woken again every ~3 s for up to 45 s.
- Sleep and shutdown: the backend follows systemd-logind (`PrepareForSleep` / `PrepareForShutdown`
  via `dbus-monitor`) and holds a short delay lock (`systemd-inhibit`, a few seconds at most) so the TV can
  be turned off before the network goes down. Steam UI events and a clock-jump detector are used as fallbacks.
- Settings are stored in Decky's plugin settings folder. The pairing key never leaves the backend.
- Manual install: `pnpm i && pnpm build`, copy `dist/`, `main.py`, `plugin.json`, `package.json` to
  `~/homebrew/plugins/NetCEC/` and run `sudo systemctl restart plugin_loader`.

License: GPL-3.0-or-later.
