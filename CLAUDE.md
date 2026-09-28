# NetMon: Restaurant Network Device Monitor

## What we're building

A network monitoring dashboard for restaurants. It checks devices on the local network (routers, switches, Wi-Fi access points, receipt printers, POS tablets) and shows whether each one is up or down, its latency, and its history. The reason it matters: a POS system is useless when the network drops, and staff need to know *what* failed, fast.

This file describes the full vision, but **we are only building Phase 1 right now.** Do not build later phases unless I ask.

## Phase 1 scope (build this)

A single local app that runs on my laptop. No cloud, no accounts, no hardware.

1. Reads a list of devices from `devices.yaml`
2. Checks every device every 30 seconds
3. Stores results in a local SQLite database
4. Serves a web dashboard at `http://localhost:8000`

### Tech stack

- Python 3.11+
- FastAPI + Uvicorn for the web server and API
- SQLite (via the standard `sqlite3` module or SQLAlchemy, your choice, keep it simple)
- `asyncio` for running checks concurrently so one slow device doesn't delay the others
- Frontend: a single `index.html` with vanilla JavaScript and Chart.js loaded from a CDN. No React, no build step.
- Use a virtual environment and a `requirements.txt`

### Device config format

```yaml
devices:
  - name: Main Router
    type: router          # router | switch | ap | printer | pos | internet
    ip: 192.168.1.1
    check: ping           # ping | tcp
  - name: Kitchen Printer
    type: printer
    ip: 192.168.1.50
    check: tcp
    port: 9100            # raw printing port on most thermal printers
  - name: Internet (Cloudflare)
    type: internet
    ip: 1.1.1.1
    check: ping
```

### Check methods

- **ping:** ICMP ping, record round-trip latency in ms. Try `icmplib` with `privileged=False` first. If that fails on my OS, fall back to calling the system `ping` command via subprocess and parsing the output. It must work on macOS, Linux, and Windows.
- **tcp:** Open a TCP connection to `ip:port` with a 2-second timeout. Latency = time to connect.
- Timeout for any check: 2 seconds. A timeout counts as a failure.

### Status logic

- A device is marked **DOWN** only after **3 consecutive failed checks**. One failure alone = still UP (this prevents flapping and false alarms).
- A device goes back to **UP** after 1 successful check.
- When a device changes state, record an **incident**: open one when it goes down, close it (set `resolved_at`) when it comes back.
- Devices that haven't been checked yet show as **UNKNOWN**.

### Database tables

- `devices`: id, name, type, ip, check_method, port, created_at (synced from `devices.yaml` on startup; add new entries, update changed ones, don't delete history for removed ones, just mark them inactive)
- `checks`: id, device_id, timestamp, is_up, latency_ms (null if failed)
- `incidents`: id, device_id, started_at, resolved_at (null while ongoing)

Add an index on `checks(device_id, timestamp)`.

**Retention:** delete raw `checks` rows older than 7 days, run once an hour. (Hourly rollups come in a later phase.)

### API endpoints

- `GET /api/devices`: all active devices with current status, last latency, last checked time, and consecutive failure count
- `GET /api/devices/{id}/history?hours=24`: check results for charting
- `GET /api/incidents?limit=50`: recent incidents, newest first, with duration
- `GET /`: serves the dashboard

### Dashboard requirements

- A grid of cards, one per device. Each card shows: name, type icon or emoji, IP, status (green UP / red DOWN / gray UNKNOWN), current latency, and "last checked X seconds ago."
- Sort DOWN devices to the top so problems are impossible to miss.
- A summary bar at the top: "12 of 14 devices up."
- Clicking a card shows a 24-hour latency line chart for that device, with failed checks marked visibly.
- A recent incidents list below the grid.
- Auto-refresh every 10 seconds (polling is fine, no websockets needed yet).
- Must be readable on a phone screen, since managers will check it from their phones.
- Clean, calm design. Dark mode support via `prefers-color-scheme`.

### Project structure

```
netmon/
  CLAUDE.md
  devices.yaml
  requirements.txt
  README.md
  netmon/
    __init__.py
    main.py        # FastAPI app, startup, background tasks
    checker.py     # ping and tcp check functions
    monitor.py     # the polling loop and status/incident logic
    db.py          # database setup and queries
    config.py      # loads devices.yaml
  static/
    index.html
  tests/
    test_status_logic.py
```

## How I want you to work

1. **Start by asking me** whether I want you to help find devices on my network. If yes, look at my machine's network settings to find my subnet and gateway, and **show me the scan command and ask permission before running it.** Only scan my own local subnet.
2. Build in small steps and run each piece before moving on: config loading, then checks (test against my router), then the database, then the loop, then the API, then the dashboard.
3. Write unit tests for the status logic (3-failure rule, recovery, incident open/close). This is the part most likely to have subtle bugs.
4. Explain what you're doing briefly as you go. I'm learning, so short explanations of *why* are welcome, but don't lecture.
5. If ping needs admin rights on my OS, tell me the options instead of silently using sudo.
6. Write a `README.md` with setup and run instructions a non-developer could follow.
7. Use git and commit after each working step with a clear message.

### Definition of done for Phase 1

- `python -m netmon.main` (or similar single command) starts everything
- The dashboard shows my real devices with live status
- Unplugging a device (or adding a fake IP) turns its card red after about 90 seconds and creates an incident
- Plugging it back in turns it green and closes the incident
- Tests pass

## Future phases (do NOT build yet, but don't design in ways that block them)

- **Phase 2: Cloud.** Split into an on-site *agent* (does the checks, pushes results) and a cloud *dashboard*. Supabase for Postgres + auth + realtime, Netlify or Vercel for hosting. Agent must buffer results locally when the internet is down and upload them when it returns.
- **Phase 3: Alerts.** Slack, SMS (Twilio), or email when a device goes down. Site heartbeat: if an agent stops reporting for 2+ minutes, alert that the whole restaurant is offline.
- **Phase 4: Deeper checks.** SNMP for switches and APs (uptime, interface errors). SNMP printer status (paper out, cover open). Checking reachability of the POS vendor's cloud (Toast, Square, etc.). Hourly rollups and uptime percentages.
- **Phase 5: Multi-site product.** Multiple restaurants, per-client logins, a prebuilt Raspberry Pi image for easy agent install, subnet auto-discovery.

Keep the check functions and the status logic separate from storage and the web layer, so they can be reused in the Phase 2 agent.
