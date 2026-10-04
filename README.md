# NetMon: Restaurant Network Monitor

NetMon watches the devices on your restaurant's network (router, switches,
Wi-Fi access points, receipt printers, POS tablets) and shows on one web page
which ones are working and which aren't.

- Every device is checked every **30 seconds**.
- A device turns **red (DOWN)** after **3 failed checks in a row** (about 90
  seconds), so a single hiccup doesn't cause a false alarm.
- It turns **green (UP)** again as soon as one check succeeds.
- Every outage is logged as an **incident** with how long it lasted.

Everything runs on your own computer. No cloud, no accounts.

---

## 1. One-time setup

You need **Python 3.11 or newer**.

**Windows:** open *PowerShell* and run:

```powershell
winget install -e --id Python.Python.3.12
```

Then close and reopen PowerShell.
**macOS:** install from <https://www.python.org/downloads/>.
**Linux:** use your package manager (for example `sudo apt install python3 python3-venv`).

Then, in a terminal **inside the NetMon folder**:

**Windows (PowerShell):**

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

This creates a private folder (`.venv`) holding NetMon's add-ons, so nothing
else on your computer is affected.

## 2. Tell NetMon which devices to watch

The first time NetMon starts, it creates `devices.yaml` with three example
devices. Replace them with your own in either of these ways.

### The easy way: from the dashboard

Start NetMon (step 3), open <http://localhost:8000> **on the NetMon computer**,
and click **Manage devices** (top right).

- **+ Add device**: fill in a name, type and IP address.
  Receipt printers are set to *TCP port 9100* automatically; everything else uses *Ping*.
- Click **Test** to check that the device answers *before* saving.
- **Save**. The device appears straight away and is checked immediately.
- **Edit** changes a device (renaming keeps its history); **Remove** stops monitoring it.

Changes are saved into `devices.yaml` for you, with no restart needed.

> For safety, devices can only be changed from the NetMon computer itself.
> Phones and other computers (with `--lan`) can view the dashboard but not
> change anything, and other websites can't change it through your browser.

**Finding a device's IP address:** print a printer's self-test page (usually:
hold the feed button while switching it on), or look at the list of connected
devices in your router's admin page. In the router, also turn on **IP
reservation** for each monitored device so its address never changes;
otherwise NetMon will be checking the wrong address after a router restart.

### By hand: editing devices.yaml

Open **`devices.yaml`** in any text editor (Notepad is fine). Each device looks like this:

```yaml
  - name: Kitchen Printer    # any name you like (must be unique)
    type: printer            # router | switch | ap | printer | pos | internet
    ip: 192.168.1.50         # the device's address on your network
    check: tcp               # ping, or tcp for devices that don't answer ping
    port: 9100               # only for tcp: 9100 works for most receipt printers
```

Keep the two-space indents exactly as shown. If something is wrong, NetMon
tells you which device and what to fix when it starts.

**Tips**
- Most devices: use `check: ping`.
- Receipt printers often ignore ping. Use `check: tcp` with `port: 9100`.
- Keep the `Internet (Cloudflare)` entry. It tells you if the internet itself is down.
- If you remove a device, its history is kept. It just stops being shown, and
  adding it back with the same name brings the history back.

Restart NetMon after editing the file by hand. (Changes made from the
dashboard don't need a restart.) Saving from the dashboard rewrites the file
neatly, so any extra comments you typed into it will be replaced.

## 3. Start NetMon

**Windows:**

```powershell
.\.venv\Scripts\python.exe -m netmon.main
```

**macOS / Linux:**

```bash
.venv/bin/python -m netmon.main
```

Then open **<http://localhost:8000>** in your browser. Leave the terminal
window open; closing it stops NetMon. Press `Ctrl+C` to stop it yourself.

### Viewing on your phone

Start NetMon with `--lan` so other devices on the same Wi-Fi can reach it:

```powershell
.\.venv\Scripts\python.exe -m netmon.main --lan
```

- **Windows** will ask whether to allow Python through the firewall. Allow it
  on **Private networks** only.
- Find your computer's address: run `ipconfig` (Windows) and look for
  *IPv4 Address*, e.g. `192.168.50.82`.
- On your phone, open `http://192.168.50.82:8000` (using your own address).

The dashboard has no password, so only use `--lan` on a network you trust
(your staff/office network, **not** the guest Wi-Fi).

## 4. Using the dashboard

- **Top bar**: "12 of 14 devices up". It turns red when anything is down.
- **Cards**: one per device. Down devices always sort to the top.
  "2 missed checks" in yellow means the device is struggling but hasn't hit
  the 3-strike limit yet.
- **Tap a card** for a 24-hour latency chart. Red ✕ marks are failed checks.
- **Recent incidents**: each outage, when it started, and how long it lasted.
- The page refreshes itself every 10 seconds. If the red "Can't reach NetMon"
  banner appears, NetMon on the laptop has stopped or the laptop is off
  the network.

### The weekly report

Tap **Weekly report** (top right) for the last 7 days: overall uptime, how
many outages there were, total downtime, and a line for every device and every
outage. **Print or save as PDF** turns it into a page you can hand to the owner
or send to your internet provider as evidence.

"NetMon was watching" shows how much of the week NetMon was actually running.
If it was switched off for a while, outages in that time weren't seen, so the
report says so rather than claiming 100%.

The chart needs the internet to load its drawing library the first time. If
the internet is down, the status cards still work; only the chart is missing.

## 5. Get alerts on your phone

NetMon can send a push notification when a device goes down and again when it
comes back. Each "down" alert says what to try, for example *"Check the
printer is on, has paper, and its lid is closed."*

1. Install the free **ntfy** app from the App Store or Google Play.
2. In the NetMon folder, copy `alerts.example.yaml` to a new file named
   `alerts.yaml`.
3. Open `alerts.yaml` and change `ntfy_topic` to a long name nobody could
   guess (anyone who knows it can read your alerts). Set `site_name` to your
   restaurant's name.
4. In the ntfy app, tap **+** and subscribe to that same topic name.
5. Check it works:

   ```powershell
   .\.venv\Scripts\python.exe -m netmon.main --test-alert
   ```

   A "TEST: DOWN: Test printer" notification should appear on your phone.
6. Restart NetMon. It now says `alerts: on` when it starts.

Every **Monday at 9:00** you also get a short summary of last week, e.g.
*"Last 7 days: 3 outages, 22 min down in total. Kitchen Printer: 2 outages
(15 min)."* Turn it off with `weekly_summary: false` in `alerts.yaml`.

To also post to a Slack or Discord channel, add that channel's incoming
webhook link as `webhook_url` in `alerts.yaml`.

**If the internet itself goes down,** alerts can't get out. NetMon keeps them
and sends them as soon as the connection is back, marked "Sent late".

## 6. Leave it running at the restaurant

For a restaurant you want NetMon on a small computer that stays on, starts
NetMon by itself after a power cut, and restarts it if anything goes wrong.

### Recommended: a Raspberry Pi

A Raspberry Pi 4 or 5 (about $60–100 with power supply and SD card) is cheap,
silent and uses very little power.

1. Install **Raspberry Pi OS** with the official *Raspberry Pi Imager*. In its
   settings, turn on SSH and set your Wi-Fi or plug the Pi into the router
   with a cable (more reliable).
2. Copy the NetMon folder onto the Pi, or download it:
   `git clone https://github.com/p00rmanS/netmon.git`
3. In that folder, run:

   ```bash
   bash scripts/install-linux.sh
   ```

   It asks for your password once, sets everything up, and prints the
   dashboard address to open on your phone (e.g. `http://192.168.1.23:8000`).
4. Copy `alerts.yaml` over too (see step 5) and restart with
   `sudo systemctl restart netmon`.

To remove it again: `bash scripts/install-linux.sh --uninstall`.

### On a Windows computer that stays on

From the NetMon folder (after step 1):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
```

NetMon then starts quietly in the background whenever you log in to Windows.
Make sure the computer is set to never sleep. To remove it, run the same
command with `-Uninstall` on the end.

### Is it still running?

- Open `http://<NetMon computer>:8000/api/health`. `"ok": true` means it's
  checking devices normally.
- With alerts on, you get a **"NetMon started"** notification every time it
  starts. If you didn't restart it yourself, the power probably went off.
- Everything it does is written to `netmon.log` in the NetMon folder (kept
  small automatically).

## 7. Testing that it works

Add a fake device to `devices.yaml`, using an address nothing is using:

```yaml
  - name: Fake Printer
    type: printer
    ip: 192.168.50.250
    check: tcp
    port: 9100
```

Restart NetMon. Within about 90 seconds the card turns red and an incident
appears (and, if alerts are on, your phone gets a notification).
Remove it again when you're done.

## Common questions

**Does ping need admin rights?** Not on Windows or macOS. On some Linux
systems it does. NetMon then uses the system `ping` command automatically,
which doesn't need them.

**Where is the data stored?** In `netmon.db` in the NetMon folder. Detailed
check results are kept for 7 days; incidents are kept forever. Delete the file
to start fresh (with NetMon stopped).

**Port 8000 is already in use.** Start with a different port, e.g.
`python -m netmon.main --port 8080`, and open `http://localhost:8080`.

## For developers

```powershell
.\.venv\Scripts\python.exe -m pytest
```

| File | Purpose |
|---|---|
| `netmon/config.py` | Loads and validates `devices.yaml` |
| `netmon/checker.py` | Ping and TCP checks (no database/web code, reusable by a future agent) |
| `netmon/monitor.py` | Status rules (3-failure DOWN, 1-success UP, incidents) and the polling loop |
| `netmon/report.py` | Weekly report: downtime, uptime and coverage per device (no database/web code) |
| `netmon/alerts.py` | Alert messages with "what to do" tips, and delivery (ntfy, Slack/Discord) with retry |
| `netmon/db.py` | SQLite schema and queries |
| `netmon/main.py` | Web server, API and startup |
| `static/index.html` | The dashboard |
| `static/report.html` | The weekly report page |

### The website (GitHub Pages)

`site/` holds the public landing page and `site/demo-api.js`, which fakes the
API with simulated devices so the real dashboard runs without a server.
`python scripts/build_site.py` builds everything into `_site/`; on every push
to `main`, `.github/workflows/pages.yml` runs the tests and publishes it.
The website is only a demo: real monitoring always runs on the restaurant's
own network.

API: `GET /api/report?days=7`, `GET /api/health`, `GET /api/devices`, `GET /api/devices/{id}/history?hours=24`,
`GET /api/incidents?limit=50`, `GET /api/meta`.
Editing (from the NetMon computer only): `POST /api/devices`,
`PUT /api/devices/{id}`, `DELETE /api/devices/{id}`, `POST /api/check` (test without saving).
