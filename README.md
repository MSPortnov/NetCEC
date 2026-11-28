# NetCEC

**English** · [Русский](README.ru.md)

**NetCEC controls Sony Bravia and Android/Google TVs over your home network**, so you do not need HDMI-CEC.
Sony uses its IP API; other Android/Google TVs use ADB when network debugging is available. On wake, the TV switches
to the PC's HDMI. Pressing PS while the PC is already awake returns the TV from another input to the configured
HDMI. On sleep or shutdown, the TV goes home or turns off. NetCEC is a [Decky Loader](https://decky.xyz/) plugin for Game Mode.

## What it can do

- **On wake-up:** turns the TV on and switches it to your HDMI input when the box starts or wakes from sleep.
- **Press PS to return to the PC:** while the box is awake, switches the TV from another input to the configured
  HDMI. If a DualSense Edge disconnected to save power, pressing PS reconnects it and then returns the TV to the PC.
- **When going to sleep** and **when shutting down:** you choose one of two actions: send the TV to its Android TV
  home screen (so it does not show "No signal") or turn it off. A reboot never touches the TV.
- **Leaves the TV alone if someone is watching something else:** both actions only run if the TV is on your
  HDMI port at that moment.
- **Choose which USB devices can wake the box,** for example the Bluetooth adapter yes, the mouse no.
- **English and Russian** interface (follows the Steam language, or pick it manually).

- **Can show what it did:** switch on "Action journal" and the panel lists the last actions (time, what happened,
  how long the switch took). It is off by default, and while it is off nothing is recorded.
- **Follows the TV if its address changes** (the router gave it a new one): it is found again by its MAC address.
  Still, it is best to reserve a fixed address for the TV in the router.
- **No pop-up notifications:** you see the result on the TV and in the list.

## What you need

- A SteamOS device with Decky Loader installed.
- A TV on the same network as the box, one of:
  - **Sony Bravia** (Android TV): controlled through Sony's IP control with a PIN. Tested.
  - **Android TV / Google TV models with ADB network debugging:** controlled over ADB. TCL has a built-in HDMI
    input map; other TVs can learn their HDMI input IDs when they expose the active input over ADB.

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

1. **On the TV.**
   - *Sony Bravia:* open Settings > Network > Home network setup > IP control. Set *Authentication* to
     **Normal** and turn on **Remote start**.
   - *Android TV (TCL and others):* turn on Developer options, then **Network debugging** (USB debugging).
2. **In NetCEC:** with the TV turned on, press **Find TV on the network** (the plugin also searches by itself
   the first time you open it) and pick your TV from the list. If there is only one result, it is selected
   automatically. If the search finds nothing, enter the IP address from the TV's network settings. Once
   a TV is selected, the search button becomes the pairing button. Clear the IP field to search again.
3. - *Sony:* press **Show PIN on TV**. A code appears on the TV screen. Type it into **PIN shown on TV** and
     press **Confirm PIN**.
   - *Android TV:* press **Connect**. The TV asks "Allow USB debugging?": choose **Always allow** (the plugin
     waits for 25 seconds; press Connect again if you were late).
4. The status line shows *Paired*. (The list of found TVs shows "Android TV" next to the TVs that are connected
   over ADB; for Sony nothing changes.)
5. Pick the **HDMI port** the box is plugged into. Then, in the three sections *On wake-up*, *When going to sleep*
   and *When shutting down*, choose what the TV should do. Under every choice the plugin shows in one sentence
   what it will do. For ADB, press **Learn HDMI** for the port you selected.

The pairing renews itself every time it is used. If it gets lost (for example you removed the device on the
TV), the status will ask you to pair again.

### Android TV: what to know

- Sony automatically uses Sony IP control; discovered Android/Google TVs use ADB. ADB network debugging must
  be available on port 5555; the newer Wireless debugging mode with a separate pairing port is not supported yet.
- On a paired Android/Google TV, press **Learn HDMI 4** if you selected HDMI 4, for example. The TV opens its home
  screen and then switches directly to the selected port. The plugin saves the input ID when two consecutive readings
  agree. Some models do not expose their current HDMI input over ADB.
- On wake, the plugin switches to the selected HDMI. After learning, it also checks whether the TV actually shows
  that input; without confirmation it reports that the command was sent but not confirmed.

## Trying it without a TV

No Sony Bravia at hand? You can still check that the plugin loads and works:

- Open NetCEC from the Quick Access menu. With no IP address the status says *Not configured*; with an
  address typed in, it says *Not paired*.
- Press **Find TV on the network**: with no Sony Bravia on your network the status says *Nothing found*.
- Type any IP address and press **Show PIN on TV**: with no TV there, the status reports *No connection*
  (the request failed cleanly, nothing else happens).
- The **Wake sources** list shows your USB devices. Switching a device off or on really changes its
  wake setting, and it is restored after a restart.
- In **When going to sleep** the "Sleep hook" line should say *active*, and *lock held* once a TV is paired.
- With "Action journal" switched on, the list under it shows every wake-up, sleep and shutdown with the time and the
  result.
- Change the **Language** and check that the interface and messages follow.
- The log in `~/homebrew/logs/NetCEC/` shows what the plugin did at start, sleep and resume.

## If something doesn't work

- **The TV is not found.** Turn the TV on first. The TV and the SteamOS machine must be on the same network
  (not in separate Wi-Fi/VLAN segments); otherwise enter the IP address by hand. The log in
  `~/homebrew/logs/NetCEC/` lists every device that answered the search.
- **The TV does not turn on.** Check that *Remote start* is enabled on the TV, and that the TV keeps its
  network connection in standby (a "network standby" or similar option in the TV settings).
- **The status says "Pairing lost or expired".** Press *Unpair TV* and pair again (steps 2 to 4).
- **The TV does not react after the router restarted.** The plugin looks for the TV again by its MAC address when
  it does not answer for a few seconds, and says so in the action journal (if it is switched on). If it still fails, reserve a fixed
  address for the TV in the router and type that address in the plugin.
- **The settings are dimmed.** Wake-up, sleep and shutdown only work for a paired TV: pair it first (steps 2 to 4).
- **Nothing happens on sleep or shutdown.** The actions only run for a paired TV (the status says *Paired*), and the
  "Sleep hook" line in the plugin should say *active*.
- **The TV stays on when the box sleeps or shuts down.** The TV is only touched when it shows your HDMI port; it is
  left alone if it shows a different input, the home screen, or did not answer in time. The log line
  `leave: input check ...` says what the TV answered. Check that the HDMI port in the plugin is the one the box
  is plugged into.
- **Logs:** `~/homebrew/logs/NetCEC/`.

## For developers

- Backend: `main.py` (Python, runs as root: it writes USB wake settings and takes a systemd lock).
  Frontend: `src/index.tsx` (React, built with `pnpm build` into `dist/`).
- TV discovery: SSDP (UPnP) multicast search; a device is listed only if its description shows a Sony
  manufacturer and the Scalar Web API, and the description is fetched only from the device that answered.
- TV control: Sony Bravia IP Control (REST + IRCC), PIN pairing, Wake-on-LAN as a backup wake-up.
- On start and on resume nothing is asked first and nothing is waited for: the remote's Back key (it ends a
  running screensaver) and the input switch go out at the same moment (two requests at once for Sony; one shell
  line with Back in the background for an Android TV, whose power state is asked first because a sleeping one
  would swallow the command). Then the plugin asks the TV every 0.4 s until it reports the port: after a
  resume the TV needs a few seconds for that, and a repeated command only restarts its switching. The command is sent
  once more only if the TV has not confirmed after 8 s; after that the result is "sent, not confirmed".
  A TV that does not answer is woken (power on + Wake-on-LAN) every ~3 s for up to 45 s. Every switch writes
  one log line with the time of each step and what the TV said.
- Sleep and shutdown: the backend follows systemd-logind (`PrepareForSleep` / `PrepareForShutdown`
  via `dbus-monitor`) and holds a short delay lock (`systemd-inhibit`, a few seconds at most) so the TV can
  be turned off before the network goes down. Steam UI events and a clock-jump detector are used as fallbacks.
  A new sleep or shutdown cancels pending wake commands. For shutdown, NetCEC needs a confirmed power-off or halt.
  Each real wake-up is handled exactly once: the kernel counts finished suspend attempts
  (`/sys/power/suspend_stats/success` + `fail`), and a source reporting the same count again is a duplicate
  (a 20 s timer is the fallback if the counter is missing; the log says which one is used).
- The action journal is `events.json` next to the settings (20 entries, the panel shows 5). It is off by default;
  while it is off nothing is written, and switching it off deletes the file. (The technical log in Decky's
  folder is always written: it is what the diagnostics are taken from.) If the TV
  does not answer for 6 s and its MAC address is known, one network search looks for a Sony TV with that MAC and
  updates the saved address.
- Settings are stored in Decky's plugin settings folder (`settings.json`, readable by the owner only). The file is
  written atomically and the last good version is kept as `settings.json.bak`; a damaged file is moved to
  `settings.json.corrupt` and the backup is used, so the pairing is not lost. The pairing key never leaves the
  backend, and every value coming from the UI is validated.
- Tests (no TV and no network needed): `python3 -m unittest discover -s tests`. They run in CI on every push.
- Build from source and install from another computer (macOS or Linux; Node.js is downloaded automatically
  if missing, and pnpm is fetched on the fly):

      git clone https://github.com/MSPortnov/NetCEC.git
      bash NetCEC/deploy.sh 192.168.1.101

  To update: `cd NetCEC && git pull && bash deploy.sh`. The SSH user is `deck` unless you set
  `DECK_USER=name`; `bash deploy.sh <IP> logs` saves diagnostic logs to `~/Downloads`.
- Decky runs plugins with a trimmed-down Python (parts of the standard library, e.g. `xml.etree`, are missing).
  `python3 tools/check_imports.py` fails if `main.py` imports a module that is not on the list of modules known
  to work there; the release build runs it too. Test a new import on a real device before adding it to the list.
- Android TV: `py_modules/netcec_adb.py` is a small ADB client of our own (RSA key made on first use and kept
  in the settings folder, 0600; the public key is offered to the TV and the user accepts it once). It needs no
  third-party library, which Decky's Python could not install anyway. If it cannot be loaded, only Android TV
  support is lost; Sony is unaffected. The drivers are chosen by the `driver` setting (`sony` or `adb`).
- Release: raise `version` in `package.json`, commit, then `git tag v1.2.3 && git push origin v1.2.3`.
  GitHub Actions builds the plugin and attaches `NetCEC.zip` (always this name, plus a `.sha256` file) to a
  new release; the build fails if the tag does not match `package.json`.

License: GPL-3.0-or-later. Third-party components and icon credits: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
