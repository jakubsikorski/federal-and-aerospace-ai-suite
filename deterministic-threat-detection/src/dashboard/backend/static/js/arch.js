// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// Architecture overlay: the four machines around the TSN switch, with the traffic of both use cases and the
// background load drawn as animated lines. Speed, width and labels come from the live status stream
// (app.js calls TsnArch.update(msg) for every message).
(function () {
  'use strict';

  const NS = 'http://www.w3.org/2000/svg';
  const modal = document.getElementById('arch-modal');
  const svg = document.getElementById('arch-svg');
  const openBtn = document.getElementById('toggle-arch');
  if (!modal || !svg || !openBtn) return;

  const UC1 = '#4da3ff', UC2 = '#3ecf8e', LOAD = '#ef5b5b', CTL = '#6b7886';

  // id, colour, path (orthogonal; the middle part runs under the switch box), label position
  let FLOWS = [];
  const baseFlows = () => [
    { id: 'uc1-m2', eg: 'M580 235 H610 V75 H740', c: UC1, d: 'M260 70 H300 V235 H610 V75 H740', lx: 264, ly: 63 },
    { id: 'uc1-m3', eg: 'M580 250 H622 V100 H740', c: UC1, d: 'M260 435 H312 V250 H622 V100 H740', lx: 264, ly: 451 },
    { id: 'uc2-m2', eg: 'M580 265 H634 V155 H740', c: UC2, d: 'M260 130 H324 V265 H634 V155 H740', lx: 264, ly: 123 },
    { id: 'uc2-m3', eg: 'M580 280 H646 V180 H740', c: UC2, d: 'M260 495 H336 V280 H646 V180 H740', lx: 264, ly: 511 },
    { id: 'load', c: LOAD, d: 'M740 480 H690 V335 H500 V305 H670 V270 H740', lx: 736, ly: 498, anchor: 'end' },
  ];

  // SOURCE=basler: the UC2 cameras are wired straight to the switch (VLAN 3), not served from machine-2/3
  const CAMERAS = {
    basler: { name: 'Basler camera', sub: 'GigE Vision', unit: 'GigE Vision' },
  };
  const camFlows = () => baseFlows().map((f) => {
    if (f.id === 'uc2-m2') return { ...f, d: 'M435 420 V265 H634 V155 H740', lx: 435, ly: 516, anchor: 'middle' };
    if (f.id === 'uc2-m3') return { ...f, d: 'M565 420 V280 H646 V180 H740', lx: 565, ly: 516, anchor: 'middle' };
    return f;
  });

  const legendFor = (cam) => [[UC1, 'Use case 1: inference over MQTT (latency)'],
    [UC2, cam ? `Use case 2: ${cam.unit} cameras on the switch (tracking)` : 'Use case 2: RTSP video (tracking)'],
    [LOAD, 'Background load, VLAN 5'], [CTL, 'Control, lab network (not TSN)']];

  function el(name, attrs, text) {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const add = (parent, name, attrs, text) => parent.appendChild(el(name, attrs, text));

  const refs = {};   // live elements by key

  function box(x, y, w, h, cls) { add(svg, 'rect', { x, y, width: w, height: h, rx: 8, class: cls || 'node' }); }
  function text(x, y, s, cls, anchor) { return add(svg, 'text', { x, y, class: cls, 'text-anchor': anchor || 'start' }, s); }

  function ptpClock(cx, cy) {
    const clk = add(svg, 'g', { class: 'clock', transform: `translate(${cx} ${cy})` });
    add(clk, 'title', {}, 'Clock synchronised with PTP (IEEE 1588)');
    add(clk, 'circle', { r: 12, cx: 0, cy: 0, class: 'hit' });
    add(clk, 'circle', { r: 6, cx: 0, cy: 0 });
    add(clk, 'path', { d: 'M0 -3.5V0L2.5 1.5' });
    add(clk, 'text', { x: -11, y: 4, class: 'ptp', 'text-anchor': 'end' }, 'PTP');
  }

  function machine(key, x, y, w, h, title, sub, metricsY) {
    box(x, y, w, h);
    add(svg, 'circle', { cx: x + w - 16, cy: y + 18, r: 4.5, class: 'dot', id: `dot-${key}` });
    ptpClock(x + w - 16, metricsY - 4);
    text(x + 14, y + 22, title, 't');
    text(x + 14 + title.length * 7.6 + 8, y + 22, sub, 's');
    refs[`sys-${key}`] = text(x + 14, metricsY, 'no metrics', 's');
    refs[`dot-${key}`] = svg.querySelector(`#dot-${key}`);
  }
  function part(x, y, w, h, l1, l2) {
    box(x, y, w, h, 'node-in');
    text(x + 10, y + (l2 ? h / 2 - 3 : h / 2 + 4), l1, 'live');
    if (l2) text(x + 10, y + h / 2 + 12, l2, 's');
  }

  function build(source) {
    const cam = CAMERAS[source] || null;
    FLOWS = cam ? camFlows() : baseFlows();
    svg.replaceChildren();
    const defs = add(svg, 'defs');
    const mk = add(defs, 'marker', { id: 'arch-arrow', viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 6, markerHeight: 6, orient: 'auto' });
    add(mk, 'path', { d: 'M0 0L8 4L0 8z', fill: CTL });

    for (const f of FLOWS) add(svg, 'path', { d: f.d, class: 'rail', style: `--c:${f.c}` });
    for (const f of FLOWS) f.path = add(svg, 'path', { d: f.d, class: 'flow idle', style: `--c:${f.c}` });
    // red pulses on the switch -> machine-1 leg: packets that never arrived (only shown while data is lost)
    for (const f of FLOWS) if (f.eg) f.drop = add(svg, 'path', { d: f.eg, class: 'drop hidden' });

    // lab-network control: dashboard -> load agent on machine-4
    add(svg, 'path', { d: 'M860 350 V398', class: 'ctl', 'marker-end': 'url(#arch-arrow)' });
    text(868, 379, 'POST /rate · lab network', 's');

    // machine-2 / machine-3: the two camera machines
    for (const [key, y, cam0, d1, d2] of [['m2', 20, 'Cam_x1_0', 52, 112], ['m3', 370, 'Cam_x2_0', 417, 477]]) {
      const camName = cam0;
      machine(key, 20, y, 240, 185, `machine-${key[1]}`, cam ? `inference · ${camName}` : `camera · ${camName}`, y + 170);
      part(36, d1, 208, 36, 'DL Streamer Pipeline Server', 'UC1 · inference → MQTT');
      if (cam) part(36, d2, 208, 36, 'Test video (mediamtx + ffmpeg)', 'UC1 input · stays on this machine');
      else part(36, d2, 208, 36, 'RTSP stream (mediamtx + ffmpeg)', 'UC2 · video');
      add(svg, 'path', { d: `M140 ${d2} V${d1 + 36}`, class: 'ctl', 'marker-end': 'url(#arch-arrow)' });
    }

    if (cam) {
      // the UC2 cameras are plugged into switch ports 5 and 6 (VLAN 3)
      [[380, 'Cam_x1_0', 'port 5'], [510, 'Cam_x2_0', 'port 6']].forEach(([x, id, port], i) => {
        box(x, 420, 110, 76, 'node-in');
        text(x + 10, 442, `${cam.name} ${i + 1}`, 'live');
        text(x + 10, 460, `${cam.sub} · ${port}`, 's');
        ptpClock(x + 94, 478);
      });
    }

    // machine-1: dashboard, SceneScape, iperf3 server
    machine('m1', 740, 20, 240, 330, 'machine-1', 'dashboard · SceneScape', 232);
    part(756, 58, 208, 54, 'MQTT broker · latency tracker', 'UC1 · one-way latency');
    part(756, 135, 208, 65, 'DL Streamer + SceneScape', 'UC2 · decode · track · HOTA');
    part(756, 250, 208, 40, 'iperf3 server', 'receives the load · VLAN 5');
    part(756, 300, 208, 36, 'Dashboard (this page)');

    // machine-4: traffic injector
    machine('m4', 740, 400, 240, 155, 'machine-4', 'traffic injector', 540);
    part(756, 450, 208, 60, 'Load agent · iperf3 client', 'UDP load on VLAN 5');

    // the switch
    refs.sw = add(svg, 'rect', { x: 420, y: 215, width: 160, height: 150, rx: 8, class: 'switch' });
    text(500, 245, 'TSN switch', 't', 'middle');
    text(500, 264, 'PTP grandmaster', 's', 'middle');
    refs.qbv = text(500, 300, 'Qbv', 'live', 'middle');
    refs.qbvSub = text(500, 318, '', 's', 'middle');
    text(500, 345, cam ? 'VLAN 1 · VLAN 3 · VLAN 5' : 'VLAN 1 · VLAN 5', 's', 'middle');

    for (const f of FLOWS) refs[f.id] = text(f.lx, f.ly, '', 'live', f.anchor);
    refs.shield = text(500, 172, '', 'live', 'middle');
    refs.badge = add(svg, 'path', { d: 'M562 226l11 4v9c0 8-5 12-11 14-6-2-11-6-11-14v-9z M557 238l4 4 7-8', class: 'badge hidden', transform: 'translate(-61 -42)' });
    refs.lock = text(500, 385, '', 'live', 'middle');

    const legend = document.getElementById('arch-legend');
    legend.replaceChildren();
    for (const [c, label] of legendFor(cam)) {
      const s = document.createElement('span');
      s.innerHTML = `<i style="--c:${c}"></i>`;
      s.append(label);
      legend.appendChild(s);
    }
  }

  // loss: share of traffic lost (0..1) on the way to machine-1; lossy lines flicker and show red pulses
  function setFlow(id, { rate, label, stale, width, loss = 0, lossLabel = '', protect = false }) {
    const f = FLOWS.find((x) => x.id === id);
    const active = !stale && rate > 0;
    const lossy = active && loss > 0;
    f.path.setAttribute('class', `flow ${stale ? 'stale' : active ? (lossy ? 'lossy' : '') : 'idle'}${protect && active && !lossy ? ' protected' : ''}`);
    f.path.style.setProperty('--dur', `${Math.max(0.35, Math.min(3, 4 / Math.sqrt(Math.max(rate, 1))))}s`);
    f.path.style.setProperty('--w', String(width || 3));
    if (f.drop) {
      f.drop.setAttribute('class', lossy ? 'drop' : 'drop hidden');
      f.drop.style.setProperty('--dur', `${Math.max(0.3, 1.2 - 4 * loss)}s`);
      f.drop.style.setProperty('--o', String(Math.min(1, 0.45 + 4 * loss)));
    }
    const t = refs[id];
    t.textContent = label;
    if (lossy && lossLabel) add(t, 'tspan', { fill: '#ff7a7a', 'font-weight': 700 }, ` · ✖ ${lossLabel}`);
  }

  // Cumulative missed-message counters and camera fps, to turn them into "lost per second" / "% of frames".
  const hist = {};
  function recent(key, now, value, span = 6) {
    const h = hist[key] = (hist[key] || []).filter((p) => now - p.t <= span);
    h.push({ t: now, v: value });
    return h;
  }

  const fix = (v, n) => (typeof v === 'number' ? v.toFixed(n) : '–');

  function update(msg) {
    if (modal.classList.contains('hidden')) return;
    const src = msg.sources || {}, scene = msg.scene, tr = msg.traffic, sys = msg.system || {};
    const protect = !!(msg.qbv && msg.qbv.known !== false && (msg.qbv.enabled || msg.qbv.active));

    [['uc1-m2', 'Cam_x1_0'], ['uc1-m3', 'Cam_x2_0']].forEach(([id, cam]) => {
      const s = src[cam];
      if (!s) return setFlow(id, { rate: 0, label: 'no data' });
      // MQTT runs over TCP: a dropped packet is retransmitted, so under load messages arrive late (a burst
      // after the stall), not missing. Delay (p95) is therefore the main impairment; sequence gaps are a bonus.
      const h = recent(`gaps-${cam}`, msg.now, s.gaps || 0);
      const lost = h[h.length - 1].v - h[0].v, dt = Math.max(1, h[h.length - 1].t - h[0].t);
      const late = (s.p95_ms || 0) > 20;
      const fmt = (ms) => (ms >= 1000 ? `${fix(ms / 1000, 1)} s` : `${fix(ms, 0)} ms`);
      const loss = lost > 0 ? Math.min(0.25, lost / dt / Math.max(s.rate_hz || 1, 1)) : late ? 0.08 + Math.min(0.17, (s.p95_ms || 0) / 4000) : 0;
      setFlow(id, { protect, rate: s.rate_hz, stale: s.stalled, loss,
        lossLabel: lost > 0 ? `${fix(lost / dt, 1)} lost/s` : `delayed · p95 ${fmt(s.p95_ms)}`,
        label: s.stalled ? '✖ stalled · messages held back' : `${fix(s.rate_hz, 0)} msg/s · ${fix(s.last_ms, 1)} ms` });
    });

    [['uc2-m2', 'Cam_x1_0'], ['uc2-m3', 'Cam_x2_0']].forEach(([id, cam]) => {
      const c = scene && scene.cameras && scene.cameras[cam];
      if (!c) return setFlow(id, { rate: 0, label: scene ? 'no data' : 'off' });
      const stale = c.age_s > 3;
      const h = recent(`fps-${cam}`, msg.now, c.fps, 60);
      const peak = Math.max(...h.map((p) => p.v));
      const deficit = peak > 0 ? Math.max(0, (peak - c.fps) / peak) : 0;
      setFlow(id, { protect, rate: c.fps, stale, loss: deficit > 0.05 ? deficit : 0, lossLabel: `${fix(deficit * 100, 0)}% frames`,
        label: stale ? 'stalled' : `${fix(c.fps, 0)} fps` });
    });

    if (tr) {
      const mbps = tr.measured ? tr.measured.mbps : (tr.running ? tr.rate_mbps : 0);
      const width = 2 + 9 * Math.min(1, (tr.rate_mbps || 0) / (tr.max_mbps || 1000));
      const loss = tr.measured && tr.measured.loss_pct > 0 ? ` · ${fix(tr.measured.loss_pct, 1)}% loss` : '';
      setFlow('load', { rate: tr.running ? Math.max(mbps, 1) * 3 : 0, stale: !!tr.error, width,
        label: tr.error ? 'agent error' : tr.running ? `${fix(mbps, 0)} Mbit/s${loss}` : 'off' });
    }

    const q = msg.qbv;
    if (q && q.known !== false) {
      const on = q.enabled || q.active;
      refs.qbv.textContent = on ? 'Qbv ON' : 'Qbv OFF';
      refs.qbv.style.fill = on ? '#3ecf8e' : '#8b98a7';
      refs.qbv.style.fontWeight = on ? '800' : '400';
      refs.qbv.style.fontSize = on ? '16px' : '11px';
      refs.sw.setAttribute('class', on ? 'switch on' : 'switch');
      refs.qbvSub.textContent = on ? 'reserved critical slots' : `gate schedule · port ${q.port}`;
      refs.badge.setAttribute('class', on ? 'badge' : 'badge hidden');
      refs.lock.textContent = on && tr && tr.running ? 'Load confined to its own slot' : '';
      refs.lock.style.fill = '#ff9d9d';
      refs.shield.textContent = on && tr && tr.running ? 'Qbv protects UC1 + UC2 from the load' : !on && tr && tr.running ? 'Qbv off: load competes with UC1 + UC2' : '';
      refs.shield.style.fill = on ? '#3ecf8e' : '#ffb347';
    } else {
      refs.qbv.textContent = 'Qbv: unknown'; refs.badge.setAttribute('class', 'badge hidden'); refs.lock.textContent = '';
      refs.qbv.style.fontWeight = '400'; refs.sw.setAttribute('class', 'switch');
      refs.qbvSub.textContent = '';
    }

    for (const key of ['m1', 'm2', 'm3', 'm4']) {
      const m = sys[`machine-${key[1]}`];
      const dot = refs[`dot-${key}`];
      if (!m) { refs[`sys-${key}`].textContent = 'no metrics'; dot.setAttribute('class', 'dot'); continue; }
      refs[`sys-${key}`].textContent = `CPU ${fix(m.cpu, 0)}% · RAM ${fix(m.mem, 0)}%`
        + (typeof m.gpu === 'number' ? ` · GPU ${fix(m.gpu, 0)}%` : '');
      dot.setAttribute('class', `dot ${m.ok && m.age_s < 5 ? 'ok' : 'bad'}`);
    }
  }

  const close = () => modal.classList.add('hidden');
  openBtn.addEventListener('click', () => modal.classList.remove('hidden'));
  document.getElementById('arch-close').addEventListener('click', close);
  modal.addEventListener('click', (e) => { if (e.target === modal) close(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });

  build('video');
  // the source (video | basler) comes from the server; redraw once it is known
  fetch('api/config').then((r) => r.json()).then((c) => { if (CAMERAS[c.source]) build(c.source); }).catch(() => {});
  window.TsnArch = { update };
  // open straight away with the newest message instead of waiting for the next one
  openBtn.addEventListener('click', () => { const m = window.__dash && window.__dash.lastStatus; if (m) update(m); });
})();
