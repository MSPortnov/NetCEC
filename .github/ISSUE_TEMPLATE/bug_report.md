---
name: Bug report
about: The TV does not turn on/off, is not found, or something in the plugin does not work
title: ""
labels: bug
---

**What happens**
Describe what you see (for example: "TV is not found", "TV does not turn on after wake", "status says Pairing lost").

**What you expected**

**Setup**
- Plugin version (shown in the plugin panel / `package.json`):
- SteamOS / distro and version:
- Decky Loader version:
- Sony Bravia model and Android TV version:
- Are the TV and the SteamOS machine on the same network (Wi-Fi / cable, same router)?

**Settings**
Which actions are set for wake, sleep and shutdown, and is "Only if TV is on my HDMI port" on?

**Logs**
Plugin logs are in `~/homebrew/logs/NetCEC/`. From another computer: `bash deploy.sh ADDRESS logs` (saves `~/Downloads/netcec-diag.txt`, see the README).

> Logs can contain IP addresses of devices on your network. Look through them before posting publicly.
