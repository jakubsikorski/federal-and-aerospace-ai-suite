// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
(() => {
  const COLORS = ['#4da3ff', '#3ecf8e', '#f5a623', '#c58bff', '#ef5b5b'];
  const $ = (id) => document.getElementById(id);
  const state = { includeProcessing: false, cfg: null, series: {}, colorIdx: 0, lastNow: 0, qbvRequestedAt: 0 };

  const chart = new Chart($('chart'), {
    type: 'line',
    data: { datasets: [] },
    options: {
      animation: false, parsing: false, normalized: true, maintainAspectRatio: false,
      interaction: { mode: 'nearest', intersect: false },
      elements: { point: { radius: 0 }, line: { borderWidth: 1.8, tension: 0 } },
      scales: {
        x: { type: 'linear', ticks: { color: '#8b98a7', maxTicksLimit: 8,
               callback: (v) => `${Math.round(v - state.lastNow)} s` },
             grid: { color: '#212b36' } },
        y: { type: 'linear', beginAtZero: true, suggestedMax: 50, title: { display: true, text: 'ms', color: '#8b98a7' },
             ticks: { color: '#8b98a7' }, grid: { color: '#212b36' } },
      },
      plugins: { legend: { labels: { color: '#e6edf3' } },
        tooltip: { callbacks: { label: (c) => { const p = c.raw; const proc = p.proc === null ? '' : `, processing ${p.proc.toFixed(1)}`; return `${c.dataset.label}: network ${p.net.toFixed(1)}${proc} ms`; } } } },
    },
  });

  // ---- use case 2: HOTA chart ------------------------------------------------------------------
  const hotaChart = new Chart($('hota-chart'), {
    type: 'line',
    data: { datasets: [
      { label: 'HOTA', data: [], borderColor: '#3ecf8e', backgroundColor: '#3ecf8e', borderWidth: 2.2 },
      { label: 'DetA (detection)', data: [], borderColor: '#4da3ff', backgroundColor: '#4da3ff', borderDash: [5, 4] },
      { label: 'AssA (association)', data: [], borderColor: '#f5a623', backgroundColor: '#f5a623', borderDash: [5, 4] },
    ] },
    options: {
      animation: false, parsing: false, normalized: true, maintainAspectRatio: false,
      elements: { point: { radius: 0 }, line: { borderWidth: 1.6, tension: 0.15 } },
      scales: {
        x: { type: 'linear', ticks: { color: '#8b98a7', maxTicksLimit: 8, callback: (v) => `${Math.round(v - state.lastNow)} s` },
             grid: { color: '#212b36' } },
        y: { min: 0, max: 1, ticks: { color: '#8b98a7' }, grid: { color: '#212b36' } },
      },
      plugins: { legend: { labels: { color: '#e6edf3' } } },
    },
  });
  const HOTA_HISTORY_S = 300;

  function addHotaPoints(points) {
    for (const [t, hota, det, ass] of points) {
      const have = hotaChart.data.datasets[0].data;
      if (have.length && t <= have[have.length - 1].x) continue;  // already drawn (the stream resends a few)
      hotaChart.data.datasets[0].data.push({ x: t, y: hota });
      hotaChart.data.datasets[1].data.push({ x: t, y: det });
      hotaChart.data.datasets[2].data.push({ x: t, y: ass });
    }
    const cutoff = state.lastNow - HOTA_HISTORY_S;
    for (const ds of hotaChart.data.datasets) while (ds.data.length && ds.data[0].x < cutoff) ds.data.shift();
  }

  function renderHota(msg) {
    const sc = msg.scene, h = msg.hota;
    const off = $('uc2-offline');
    if (!sc) {
      off.textContent = 'Use case 2 is not enabled in this dashboard (SCENE_ENABLED=0 or the ground truth is missing).';
    } else if (!sc.connected) {
      off.textContent = 'The SceneScape stack is not reachable on this machine. Start use case 2 (make up) to see tracking quality.';
    } else if (sc.scene_age_s === null || sc.scene_age_s > 5) {
      off.textContent = 'No tracked output from SceneScape for the last few seconds - the cameras or the pipelines are stalled.';
    } else {
      off.textContent = '';
    }
    off.classList.toggle('hidden', !off.textContent);
    if (sc) {
      $('scene-rate').innerHTML = `${sc.scene_hz}<small> updates/s</small>`;
      const cams = Object.entries(sc.cameras || {}).map(([c, v]) => `${c.replace('Cam_', '')} ${Math.round(v.fps)}${v.age_s > 3 ? '!' : ''}`);
      const rs = sc.pipelines ? Object.values(sc.pipelines.restarts).reduce((a, b) => a + b, 0) : 0;
      const camText = `${cams.join(' · ') || '–'} fps` + (rs ? ` · ${rs} restarts` : '');
      $('scene-cam').textContent = camText;
      $('scene-cam').title = `Frames per second per camera ('!' = stalled)` + (rs ? `; the pipeline watchdog restarted a camera pipeline ${rs} time(s)` : '');
    }
    if (h) {
      $('hota-value').textContent = h.HOTA.toFixed(2);
      $('hota-det').textContent = `DetA ${h.DetA.toFixed(2)}`;
      $('hota-ass').textContent = `AssA ${h.AssA.toFixed(2)}`;
      $('hota-covered').textContent = `${Math.round(h.covered * 100)} %`;
      $('hota-frames').textContent = `${h.frames} frames scored`;
    } else {
      $('hota-value').textContent = '–';
      $('hota-covered').textContent = '–';
    }
    if (activeTab === 'uc2') { hotaChart.options.scales.x.min = state.lastNow - 120; hotaChart.options.scales.x.max = state.lastNow; hotaChart.update('none'); }
  }

  // ---- system utilization: one CPU/RAM/GPU/NPU line chart per machine (same look as uav-ground-station) ----
  const SERIES = [
    { key: 'cpu', label: 'CPU %', short: 'CPU', color: '#1ad0ff' },
    { key: 'mem', label: 'RAM %', short: 'RAM', color: '#8ca0c2' },
    { key: 'gpu', label: 'GPU %', short: 'GPU', color: '#ffb347' },
    { key: 'npu', label: 'NPU %', short: 'NPU', color: '#b388ff' },
  ];
  const SYS_MAX_POINTS = 60;
  const sysPanels = new Map();
  let sysSelected = null;  // machine name -> { el, chart, vals, note }
  const fmtPct = (v) => (v === null || v === undefined ? '—' : Math.max(0, Math.min(100, v)).toFixed(0) + '%');

  function makeSysPanel(name) {
    const el = document.createElement('div');
    el.className = 'sys-machine';
    el.innerHTML = `<div class="sys-title"><span class="sys-note"></span></div>` +
      '<div class="sys-header">' + SERIES.map((s) =>
        `<span class="sys-value" data-key="${s.key}" style="color:${s.color}">${s.short} <span>—</span></span>`).join('') + '</div>' +
      '<div class="sys-chart"><canvas></canvas></div>';
    $('system-rows').appendChild(el);
    const ctx = el.querySelector('canvas').getContext('2d');
    const datasets = SERIES.map((s) => {
      const g = ctx.createLinearGradient(0, 0, 0, 300);
      g.addColorStop(0, s.color + '33'); g.addColorStop(1, s.color + '05');
      return { label: s.label, data: [], borderColor: s.color, backgroundColor: g, tension: 0.35,
        fill: s.key === 'cpu' || s.key === 'mem', pointRadius: 0, borderWidth: 2, spanGaps: true };
    });
    const chart = new Chart(ctx, { type: 'line', data: { labels: [], datasets }, options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: { x: { display: false },
        y: { suggestedMin: 0, suggestedMax: 100, grid: { color: 'rgba(230, 237, 243, 0.08)' }, ticks: { color: '#8a97a6' } } },
      plugins: { legend: { display: false } } } });
    const panel = { el, chart, note: el.querySelector('.sys-note'),
      vals: Object.fromEntries(SERIES.map((s) => [s.key, el.querySelector(`[data-key="${s.key}"]`)])) };
    sysPanels.set(name, panel);
    new ResizeObserver(() => chart.resize()).observe(el.querySelector('.sys-chart'));
    const sel = $('system-select');
    sel.add(new Option(name, name));
    sel.hidden = sysPanels.size < 2;
    if (sysSelected === null) sysSelected = name;  // the first machine is shown until another is picked
    el.classList.toggle('hidden', name !== sysSelected);
    return panel;
  }

  $('system-select').addEventListener('change', (e) => {
    sysSelected = e.target.value;
    for (const [n, p] of sysPanels) p.el.classList.toggle('hidden', n !== sysSelected);
    sysPanels.get(sysSelected).chart.resize();
  });

  function renderSystem(system) {
    $('system-empty').classList.toggle('hidden', !!system && Object.keys(system).length > 0);
    if (!system) return;
    for (const [name, m] of Object.entries(system)) {
      const p = sysPanels.get(name) || makeSysPanel(name);
      const mem = m.mem_total ? `${(m.mem_used / 1e9).toFixed(1)} of ${(m.mem_total / 1e9).toFixed(1)} GB` : '';
      const temp = m.temp !== null && m.temp !== undefined ? `${Math.round(m.temp)} °C` : '';
      p.note.textContent = m.ok ? [mem, temp].filter(Boolean).join(' · ')
        : (m.age_s === null ? 'no data yet' : 'no data for ' + Math.round(m.age_s) + ' s');
      p.el.classList.toggle('stale', !m.ok);
      const c = p.chart;
      c.data.labels.push(new Date().toLocaleTimeString());
      SERIES.forEach((s, i) => {
        const v = m.ok && m[s.key] !== undefined ? m[s.key] : null;
        c.data.datasets[i].data.push(v);
        p.vals[s.key].lastChild.textContent = fmtPct(v);
        // GPU and NPU are not present on every machine: hide a value that has never been reported
        if (v !== null) p.vals[s.key].dataset.seen = '1';
        p.vals[s.key].style.display = s.key === 'cpu' || s.key === 'mem' || p.vals[s.key].dataset.seen ? '' : 'none';
      });
      while (c.data.labels.length > SYS_MAX_POINTS) { c.data.labels.shift(); c.data.datasets.forEach((d) => d.data.shift()); }
      c.update('none');
    }
  }

  // ---- tabs ------------------------------------------------------------------------------------
  let activeTab = 'uc1';
  function showTab(name) {
    activeTab = name === 'uc2' ? 'uc2' : 'uc1';
    for (const t of document.querySelectorAll('.tab')) t.setAttribute('aria-selected', String(t.dataset.tab === activeTab));
    $('view-uc1').classList.toggle('hidden', activeTab !== 'uc1');
    $('view-uc2').classList.toggle('hidden', activeTab !== 'uc2');
    location.hash = activeTab;
    chart.resize(); hotaChart.resize();
    setVideo(activeTab === 'uc2');
  }

  // Motion-JPEG of the frames the pipelines decoded (served by this backend). Only streamed while the
  // tab is open: opening makes the backend request frames, a blank src stops the requests.
  function setVideo(on) {
    const decoded = { 'decoded-cam1': 'Cam_x1_0', 'decoded-cam2': 'Cam_x2_0' };
    for (const [id, cam] of Object.entries(decoded)) {
      const el = $(id);
      if (on) { if (!el.getAttribute('src')) el.setAttribute('src', `api/frames/${cam}.mjpg`); }
      else el.removeAttribute('src');
    }
  }
  for (const t of document.querySelectorAll('.tab')) t.addEventListener('click', () => showTab(t.dataset.tab));
  window.addEventListener('hashchange', () => showTab(location.hash.replace('#', '')));

  function series(name) {
    if (!state.series[name]) {
      const color = COLORS[state.colorIdx++ % COLORS.length];
      const ds = { label: name, data: [], borderColor: color, backgroundColor: color };
      chart.data.datasets.push(ds);
      state.series[name] = { ds, color, kpi: makeKpi(name, color) };
    }
    return state.series[name];
  }

  function makeKpi(name, color) {
    const el = document.createElement('div');
    el.className = 'kpi';
    el.style.setProperty('--c', color);
    el.innerHTML = `<div class="kpi-name"><span></span><span class="tag"></span></div>
      <div class="kpi-value">–<small> ms</small></div>
      <div class="kpi-row"><span class="mean"></span><span class="p95"></span><span class="max"></span></div>
      <div class="kpi-row"><span class="proc"></span><span class="rate"></span><span class="gaps"></span></div>`;
    el.querySelector('.kpi-name span').textContent = name;
    $('kpis').appendChild(el);
    return el;
  }

  const fmt = (v) => (v === null || v === undefined ? '–' : v >= 100 ? v.toFixed(0) : v.toFixed(1));

  function renderKpis(sources) {
    for (const [name, s] of Object.entries(sources)) {
      const el = series(name).kpi;
      el.classList.toggle('stalled', s.stalled);
      el.querySelector('.tag').textContent = s.stalled ? 'no data' : '';
      el.querySelector('.tag').className = 'tag' + (s.stalled ? ' tag-stalled' : '');
      el.querySelector('.kpi-value').innerHTML = `${fmt(s.last_ms)}<small> ms</small>`;
      el.querySelector('.mean').textContent = `mean ${fmt(s.mean_ms)}`;
      el.querySelector('.p95').textContent = `p95 ${fmt(s.p95_ms)}`;
      el.querySelector('.max').textContent = `max ${fmt(s.max_ms)}`;
      el.querySelector('.proc').textContent = s.proc_ms === null ? '' : `processing ${fmt(s.proc_ms)} ms`;
      el.querySelector('.rate').textContent = `${s.rate_hz} msg/s`;
      el.querySelector('.gaps').textContent = `missed ${s.gaps}` + (s.rejected ? ` · bad clock ${s.rejected}` : '');
    }
    $('empty').classList.toggle('hidden', Object.keys(sources).length > 0);
  }

  const yOf = (net, proc) => net + (state.includeProcessing && proc !== null ? proc : 0);

  // Server timestamps are used for the x axis; 'offset' hides browser/server clock differences.
  function addPoints(points, historySpan) {
    for (const [name, pts] of Object.entries(points)) {
      const ds = series(name).ds;
      for (const [t, net, proc] of pts) ds.data.push({ x: t, net, proc, y: yOf(net, proc) });
      const cutoff = state.lastNow - historySpan;
      while (ds.data.length && ds.data[0].x < cutoff) ds.data.shift();
    }
  }

  function render(now) {
    state.lastNow = now;
    chart.options.scales.x.min = now - state.cfg.history_s / 2;
    chart.options.scales.x.max = now;
    chart.update('none');
  }

  function renderControls(msg) {
    const mq = $('chip-mqtt');
    mq.textContent = msg.mqtt_connected ? 'Connected' : 'Disconnected';
    mq.className = 'chip ' + (msg.mqtt_connected ? 'chip-ok' : 'chip-bad');

    const t = msg.traffic;
    if (!state.draggingTraffic) {
      $('traffic').value = t.rate_mbps;
      $('traffic-value').textContent = `${t.rate_mbps} Mbit/s`;
    }
    const ms = t.measured;
    $('traffic-measured').textContent = ms
      ? `Arriving at machine-1: ${ms.mbps.toFixed(0)} Mbit/s, loss ${ms.loss_pct.toFixed(1)} %`
      : (t.running && t.mode === 'iperf' ? 'Waiting for the iperf3 receiver…' : '');
    const tm = $('traffic-msg');
    tm.textContent = t.error || (t.rate_mbps > 0 && !t.running ? 'Load stopped unexpectedly' : '');
    tm.classList.toggle('hidden', !tm.textContent);

    const q = msg.qbv;
    $('qbv-port').textContent = `port ${q.port}`;
    // While a change is being applied show where it is heading (disabled, "Enabling…"); once the
    // switch has been read back show what it really has. Never trust the click, trust the switch.
    // A click locks the button until the server reports 'busy' (or 4 s pass): a push that was built
    // before the request reached the server must not re-enable it for a moment.
    if (q.busy) state.qbvRequestedAt = 0;
    const requested = state.qbvRequestedAt > 0 && Date.now() - state.qbvRequestedAt < 4000;
    const known = q.known !== false;
    const shown = q.busy && q.target !== null && q.target !== undefined ? q.target : q.enabled;
    $('qbv').checked = known && shown;
    // The shaper's per-port toggle lives in the switch web page; the CLI cannot set it. Without it
    // a schedule is stored but never runs, so say so and do not offer a button that cannot work.
    const gateOff = known && q.gate === false;
    $('qbv').disabled = q.busy || q.read_only || !known || requested || gateOff;
    $('qbv-label').textContent = !known ? 'Reading switch…'
      : q.busy ? (q.target ? 'Enabling…' : 'Disabling…') : q.enabled ? 'On' : 'Off';
    const warn = $('qbv-warn');
    if (gateOff) {
      warn.innerHTML = `<strong>Qbv cannot run on port ${q.port}.</strong> Turn on the Time-aware Shaper for port ${q.port} in the ` +
        'switch web page (Layer 2 Switching → Time-aware Shaper → toggle in front of the port). ' +
        'Until then this button is disabled.';
    } else if (known && q.configured && q.active === false && !q.busy) {
      warn.innerHTML = `<strong>Schedule configured but not running.</strong> The switch has a gate list for port ${q.port} ` +
        'but is not applying it. Check the Time-aware Shaper status in the switch web page.';
    } else {
      warn.textContent = '';
    }
    warn.classList.toggle('hidden', !warn.textContent);
    document.querySelector('.switch').classList.toggle('busy', q.busy || requested);
    const qm = $('qbv-msg');
    qm.textContent = q.error || (q.read_only ? 'Read-only: switch changes are disabled' : '');
    qm.classList.toggle('hidden', !qm.textContent);
    const sched = (q.slots || []).map((sl) => `${sl.interval_us / 1000} ms: Q${sl.queues.join(',')}`).join('  |  ');
    $('qbv-schedule').textContent = q.enabled && sched ? `Schedule: ${sched}` : '';

    const mode = $('chip-mode');
    const simulated = [t.mode === 'sim' && 'load', q.mode === 'sim' && 'switch'].filter(Boolean);
    mode.textContent = simulated.length ? `simulated: ${simulated.join(' + ')}` : '';
    mode.classList.toggle('hidden', !simulated.length);
  }

  let trafficTimer;
  $('traffic').addEventListener('input', (e) => {
    state.draggingTraffic = true;
    $('traffic-value').textContent = `${e.target.value} Mbit/s`;
    clearTimeout(trafficTimer);
    trafficTimer = setTimeout(async () => {
      await post('api/traffic', { rate_mbps: Number(e.target.value) });
      state.draggingTraffic = false;
    }, 300);
  });

  $('qbv').addEventListener('change', async (e) => {
    const wanted = e.target.checked;
    state.qbvRequestedAt = Date.now();  // lock the button right away; the server state takes over
    $('qbv').disabled = true;
    $('qbv-label').textContent = wanted ? 'Enabling…' : 'Disabling…';
    const r = await post('api/qbv', { enabled: wanted });
    if (!r.ok) {
      state.qbvRequestedAt = 0;
      const m = $('qbv-msg');
      m.textContent = (await r.json()).detail || 'Request failed';
      m.classList.remove('hidden');
      e.target.checked = !wanted;
      $('qbv').disabled = false;
    }
  });

  $('log-scale').addEventListener('change', (e) => {
    const y = chart.options.scales.y;
    y.type = e.target.checked ? 'logarithmic' : 'linear';
    y.beginAtZero = !e.target.checked;
    y.min = e.target.checked ? 1 : undefined;
    chart.update('none');
  });

  $('include-proc').addEventListener('change', (e) => {
    state.includeProcessing = e.target.checked;
    for (const sr of Object.values(state.series)) for (const p of sr.ds.data) p.y = yOf(p.net, p.proc);
    chart.update('none');
  });

  $('clear').addEventListener('click', async () => {
    await post('api/reset', {});
    for (const s of Object.values(state.series)) s.ds.data.length = 0;
    chart.update('none');
  });

  function post(url, body) {
    return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  }

  async function start() {
    state.cfg = await (await fetch('api/config')).json();
    $('traffic').max = state.cfg.traffic_max_mbps;
    const hist = await (await fetch('api/history')).json();
    state.lastNow = hist.now;
    addPoints(hist.points, state.cfg.history_s);
    try {
      const h = await (await fetch('api/hota')).json();
      state.lastNow = h.now; addHotaPoints(h.points);
      $('hota-window').textContent = h.window_s;
    } catch (e) { /* use case 2 disabled */ }
    showTab(location.hash.replace('#', ''));
    window.__dash = { state, chart, hotaChart, es: null, msgs: 0, lastMsg: Date.now(), reconnects: 0 };  // also a hook for browser tests
    const connect = () => {
      const es = new EventSource('api/stream');
      window.__dash.es = es;
      es.onmessage = (ev) => onStream(JSON.parse(ev.data));
      es.onerror = () => {
        $('chip-mqtt').textContent = 'Backend: reconnecting';
        $('chip-mqtt').className = 'chip chip-bad';
      };
    };
    // A silently stalled stream (a proxy or a saturated link) never raises an error, so the charts would
    // just stop until the page is reloaded. No message for 6 s: reopen the stream.
    setInterval(() => {
      if (Date.now() - window.__dash.lastMsg > 6000) {
        window.__dash.es.close(); window.__dash.reconnects += 1; window.__dash.lastMsg = Date.now(); connect();
      }
    }, 2000);
    const onStream = (msg) => {
      window.__dash.msgs += 1; window.__dash.lastMsg = Date.now();
      state.lastNow = msg.now;
      addPoints(msg.points, state.cfg.history_s);
      addHotaPoints(msg.hota_points || []);
      renderHota(msg);
      renderSystem(msg.system);
      renderKpis(msg.sources);
      renderControls(msg);
      render(msg.now);
      window.__dash.lastStatus = msg;
      if (window.TsnArch) window.TsnArch.update(msg);
    };
    connect();
  }
  start();
})();
