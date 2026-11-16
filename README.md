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

## Installing and updating

The same command installs the plugin and updates it later (it always takes the latest release from GitHub).
Settings and the TV pairing are kept.

**On the SteamOS machine itself.** Switch to Desktop Mode, open a terminal (Konsole) and run:

    curl -fsSL https://raw.githubusercontent.com/MSPortnov/NetCEC/main/install.sh | bash

The first time you need a password for the `deck` user (set it with `passwd` if you never did): `sudo` asks for it.

**From another computer (macOS or Linux).** One time, on the SteamOS machine, turn SSH on and note its IP address:

    sudo systemctl enable --now sshd
    ip -4 addr | grep inet

Then, on your computer (put your IP address instead of `192.168.1.101`):

    ssh -t deck@192.168.1.101 'curl -fsSL https://raw.githubusercontent.com/MSPortnov/NetCEC/main/install.sh | bash'

**Without a terminal.** In Decky open Settings > General and turn on *Developer mode*. Then open *Developer*,
choose *Install Plugin from URL* and paste:

    https://github.com/MSPortnov/NetCEC/releases/latest/download/NetCEC.zip

Installing or updating restarts Decky, and NetCEC switches the TV on when it starts, so the TV may turn on.
The script is short, [read it here](install.sh) before you run it.

## Setting up

1. **On the TV:** open Settings > Network > Home network setup > IP control. Set *Authentication* to
   **Normal** and turn on **Remote start**.
2. **In NetCEC:** type the TV's IP address (you can find it in the TV's network settings).
3. Press **Show PIN on TV**. A code appears on the TV screen.
4. Type that code into **PIN shown on TV** and press **Confirm PIN**. The status line shows *Paired*.
5. Pick the **HDMI port** the box is plugged into, then choose what the TV should do on wake, sleep and shutdown.

The pairing renews itself every time it is used. If it gets lost (for example you removed the device on the
TV), the status will ask you to pair again.

## Trying it without a TV

No Sony Bravia at hand? You can still check that the plugin loads and works:

- Open NetCEC from the Quick Access menu. With no IP address the status says *Not configured*; with an
  address typed in, it says *Not paired*.
- Type any IP address and press **Show PIN on TV**: with no TV there, the status reports *No connection*
  (the request failed cleanly, nothing else happens).
- The **Wake sources (USB)** list shows your USB devices. Switching a device off or on really changes its
  wake setting, and it is restored after a restart.
- Set **When going to sleep** to any action: the "Sleep hook" line should say *active* and *lock held*.
- Change the **Language** and check that the interface and messages follow.
- The log in `~/homebrew/logs/NetCEC/` shows what the plugin did at start, sleep and resume.

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
- On start and on resume the plugin sends the wake / input-switch commands right away (2 s timeouts)
  and stops as soon as the TV shows the right port. If the TV is on another input or on its home screen,
  the remote's Home key is sent first (it ends a running screensaver) and then the input switch, with a
  0.4 s pause between them. The input switch is sent at most 3 times; a sleeping TV is woken again
  every ~3 s for up to 45 s.
- Sleep and shutdown: the backend follows systemd-logind (`PrepareForSleep` / `PrepareForShutdown`
  via `dbus-monitor`) and holds a short delay lock (`systemd-inhibit`, a few seconds at most) so the TV can
  be turned off before the network goes down. Steam UI events and a clock-jump detector are used as fallbacks.
- Settings are stored in Decky's plugin settings folder. The pairing key never leaves the backend.
- Build from source and install from another computer (macOS or Linux; Node.js is downloaded automatically
  if missing, and pnpm is fetched on the fly):

      git clone https://github.com/MSPortnov/NetCEC.git
      bash NetCEC/deploy.sh 192.168.1.101

  To update: `cd NetCEC && git pull && bash deploy.sh`. The SSH user is `deck` unless you set
  `DECK_USER=name`; `bash deploy.sh <IP> logs` saves diagnostic logs to `~/Downloads`.
- Release: raise `version` in `package.json`, commit, then `git tag v1.2.3 && git push origin v1.2.3`.
  GitHub Actions builds the plugin and attaches `NetCEC.zip` (always this name, plus a `.sha256` file) to a
  new release; the build fails if the tag does not match `package.json`.

License: GPL-3.0-or-later. Third-party components and icon credits: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
