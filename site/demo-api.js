// Demo mode for the public website (GitHub Pages has no server).
// Loaded before the dashboard's own script: it answers the dashboard's /api/...
// requests with simulated restaurant devices, so the real dashboard runs as-is.
"use strict";
(() => {
  const START = Date.now();
  const H = 3600e3, MIN = 60e3;

  // [name, type, ip, method, port, base latency ms]
  const DEVICES = [
    ["Main Router", "router", "192.168.1.1", "ping", null, 1.2],
    ["Internet (Cloudflare)", "internet", "1.1.1.1", "ping", null, 14],
    ["Back Office Switch", "switch", "192.168.1.2", "ping", null, 0.8],
    ["Dining Room Wi-Fi", "ap", "192.168.1.10", "ping", null, 2.5],
    ["Patio Wi-Fi", "ap", "192.168.1.11", "ping", null, 3.1],
    ["Kitchen Printer", "printer", "192.168.1.50", "tcp", 9100, 4.5],
    ["Bar Printer", "printer", "192.168.1.51", "tcp", 9100, 5.2],
    ["Front Counter POS", "pos", "192.168.1.60", "ping", null, 18],
    ["Bar POS", "pos", "192.168.1.61", "ping", null, 22],
    ["Server Tablet 1", "pos", "192.168.1.62", "ping", null, 35],
  ].map(([name, type, ip, check_method, port, base], i) => ({ id: i + 1, name, type, ip, check_method, port, base }));

  // Outages as [device id, minutes ago it started, minutes it lasted (null = still down)].
  const OUTAGES = [
    [6, 4.5, null],        // Kitchen Printer: down right now
    [5, 140, 11],          // Patio Wi-Fi, earlier today
    [2, 610, 6],           // Internet blip this morning
    [9, 1300, 3],          // Bar POS, yesterday
  ];

  function rand(seed) {  // small deterministic PRNG so the charts don't reshuffle
    return () => {
      seed = (seed * 1664525 + 1013904223) % 4294967296;
      return seed / 4294967296;
    };
  }

  function isDownAt(id, t) {
    return OUTAGES.some(([d, ago, len]) => {
      if (d !== id) return false;
      const start = START - ago * MIN;
      return t >= start && (len == null || t < start + len * MIN);
    });
  }

  function latency(dev, r) {
    const spike = r() < 0.006 ? 2 + r() * 3 : 1;
    return +(dev.base * (0.8 + r() * 0.5) * spike).toFixed(1);
  }

  const iso = (t) => new Date(t).toISOString();

  function devicesNow() {
    const now = Date.now();
    const r = rand(Math.floor(now / 30e3));
    return DEVICES.map((d) => {
      const since = (now / 1000 + d.id * 3) % 30;  // checked every 30 s, staggered
      const outage = OUTAGES.find(([id, , len]) => id === d.id && len == null);
      const down = !!outage;
      const failingSince = down ? START - outage[1] * MIN : null;
      return {
        id: d.id, name: d.name, type: d.type, ip: d.ip, check_method: d.check_method, port: d.port,
        status: down ? "DOWN" : "UP",
        consecutive_failures: down ? Math.floor((now - failingSince) / 30e3) + 1 : 0,
        last_latency_ms: down ? null : latency(d, r),
        last_checked: iso(now - since * 1000),
        seconds_since_check: +since.toFixed(1),
        failing_since: down ? iso(failingSince) : null,
        seconds_failing: down ? +((now - failingSince) / 1000).toFixed(1) : null,
      };
    });
  }

  function history(id, hours) {
    const dev = DEVICES.find((d) => d.id === id);
    if (!dev) return null;
    const now = Date.now(), r = rand(id * 7919);
    const step = 2 * MIN;  // a point every 2 minutes keeps the chart light
    const points = [];
    for (let t = now - hours * H; t <= now; t += step) {
      const down = isDownAt(id, t);
      points.push({ t: iso(t), is_up: !down, latency_ms: down ? null : latency(dev, r) });
    }
    return { device_id: id, name: dev.name, hours, points };
  }

  function incidents() {
    const now = Date.now();
    return OUTAGES.map(([id, ago, len], i) => {
      const d = DEVICES.find((x) => x.id === id);
      const start = START - ago * MIN, end = len == null ? null : start + len * MIN;
      return {
        id: OUTAGES.length - i, device_id: id, device_name: d.name, device_type: d.type,
        started_at: iso(start), resolved_at: end && iso(end), ongoing: end == null,
        duration_s: Math.round(((end ?? now) - start) / 1000),
      };
    });
  }

  function route(path, params) {
    if (path === "/api/meta") return { can_edit: false };
    if (path === "/api/devices") return devicesNow();
    if (path === "/api/incidents") return incidents().slice(0, +(params.get("limit") || 50));
    const m = path.match(/^\/api\/devices\/(\d+)\/history$/);
    if (m) return history(+m[1], +(params.get("hours") || 24));
    return null;
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const i = url.pathname.indexOf("/api/");
    if (i === -1) return realFetch(input, init);
    const body = (init?.method ?? "GET") === "GET" ? route(url.pathname.slice(i), url.searchParams) : null;
    await new Promise((res) => setTimeout(res, 120));  // feel like a network
    return body == null
      ? new Response(JSON.stringify({ detail: "Not available in the demo" }), { status: 404 })
      : new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  };

  // A small banner so nobody mistakes this for a real restaurant.
  document.addEventListener("DOMContentLoaded", () => {
    const bar = document.createElement("div");
    bar.setAttribute("role", "note");
    bar.style.cssText = "background:var(--text);color:var(--bg);padding:8px 16px;font-size:14px;text-align:center";
    bar.innerHTML = 'Demo with simulated devices. <a href="../" style="color:inherit;font-weight:600">About NetMon</a>';
    document.body.prepend(bar);
  });
})();
