// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// "Flight Summary": modal with a small form (host/cameras/window) that calls
// viz-backend's /api/summary (JSON, rendered inline) and exposes a matching
// /api/summary.pdf link for download. Independent of VSS -- works whenever a
// camera has been started with captioning on, since it's built entirely from
// the MQTT caption stream (see backend/caption_log.py + vss_summary.py), not
// a VSS deployment.
//
// Unlike the earlier device-based (cpu/gpu/npu) design, cameras here are a
// dynamic, per-deployment set of names (e.g. "uav-front-camera"), so the
// camera checkbox list is populated on the fly from /api/detect-pipelines
// instead of being a fixed markup block.
(() => {
  const modal = document.getElementById("summary-modal");
  const openBtn = document.getElementById("topbar-summary-btn");
  const closeBtn = document.getElementById("summary-modal-close");
  const form = document.getElementById("summary-form");
  const hostSelect = document.getElementById("summary-host-select");
  const hostInput = document.getElementById("summary-host-input");
  const cameraChecks = document.getElementById("summary-camera-checks");
  const windowInput = document.getElementById("summary-window-input");
  const highlightsInput = document.getElementById("summary-highlights-input");
  const generateBtn = document.getElementById("summary-generate-btn");
  const downloadBtn = document.getElementById("summary-download-btn");
  const body = document.getElementById("summary-body");

  let lastFocus = null;
  let inFlight = null;
  let camerasLoadedForHost = null;
  // Bumped on every populateCameras() call so a slower, superseded request
  // can tell its own response is stale and skip rendering it -- without
  // this, two in-flight requests racing could leave the modal showing the
  // wrong host's cameras, or even get stuck re-showing "Looking up
  // cameras..." if the later request's promise settles before the earlier
  // (now-irrelevant) one does.
  let camerasRequestToken = 0;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function selectedCameras() {
    return Array.from(cameraChecks.querySelectorAll('input[name="summary-camera"]:checked')).map((cb) => cb.value);
  }

  const CUSTOM_HOST_VALUE = "__custom__";

  function currentHost() {
    if (hostSelect.value && hostSelect.value !== CUSTOM_HOST_VALUE) return hostSelect.value;
    return hostInput.value.trim();
  }

  // Already-connected UAVs are known to the app (one tab per host), so offer
  // as a dropdown instead of asking to re-type -- showing the label if given
  // one, otherwise the host/IP. Falls back to a free-text IP field only if
  // nothing is connected yet, or the user explicitly wants a host that isn't
  // one of the open tabs.
  function populateHostSelect() {
    const sessions = window.UavSessions ? window.UavSessions.list() : [];
    const active = window.UavSessions ? window.UavSessions.current() : null;
    const previousSelection = hostSelect.value;
    hostSelect.replaceChildren();

    sessions.forEach(({ host, label }) => {
      const opt = document.createElement("option");
      opt.value = host;
      opt.textContent = label ? `${label} (${host})` : host;
      hostSelect.appendChild(opt);
    });
    const customOpt = document.createElement("option");
    customOpt.value = CUSTOM_HOST_VALUE;
    customOpt.textContent = "Custom IP\u2026";
    hostSelect.appendChild(customOpt);

    let nextSelection;
    if (sessions.some((s) => s.host === previousSelection)) {
      nextSelection = previousSelection; // keep user's existing choice if still valid
    } else if (active && sessions.some((s) => s.host === active)) {
      nextSelection = active; // default to the tab currently on screen
    } else if (sessions.length) {
      nextSelection = sessions[0].host;
    } else {
      nextSelection = CUSTOM_HOST_VALUE;
    }
    hostSelect.value = nextSelection;
    hostInput.classList.toggle("hidden", nextSelection !== CUSTOM_HOST_VALUE);
  }

  // Hard cap so a hung/slow /api/detect-pipelines call (e.g. the target UAV
  // just rebooted its pipeline-server and is mid-reconnect) can't leave the
  // modal stuck on "Looking up cameras..." forever with no way out other
  // than a full page reload -- it now always resolves to either the real
  // camera list or an explicit "took too long" message within this window.
  const DETECT_PIPELINES_TIMEOUT_MS = 10000;

  async function populateCameras(host, { force = false } = {}) {
    const myToken = ++camerasRequestToken;

    if (!host) {
      cameraChecks.replaceChildren(el("span", "muted", "Enter a UAV host above to list its cameras\u2026"));
      camerasLoadedForHost = null;
      return;
    }
    if (!force && camerasLoadedForHost === host) return;
    cameraChecks.replaceChildren(el("span", "muted", "Looking up cameras\u2026"));
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), DETECT_PIPELINES_TIMEOUT_MS);
    try {
      const res = await fetch(`/api/detect-pipelines?host=${encodeURIComponent(host)}`, { signal: controller.signal });
      if (myToken !== camerasRequestToken) return; // superseded by a newer call -- discard
      const data = await res.json();
      if (myToken !== camerasRequestToken) return;
      const cameras = Array.isArray(data.cameras) ? data.cameras : [];
      if (!data.reachable) {
        cameraChecks.replaceChildren(el("span", "muted", `Could not reach ${host}.`));
        camerasLoadedForHost = null;
        return;
      }
      const captioning = cameras.filter((c) => c.captioning);
      if (!captioning.length) {
        cameraChecks.replaceChildren(el("span", "muted",
          "No camera on this host is currently captioning -- start a pipeline with captioning enabled first."));
        camerasLoadedForHost = host;
        return;
      }
      cameraChecks.replaceChildren();
      captioning.forEach((cam, i) => {
        const label = el("label");
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.name = "summary-camera";
        cb.value = cam.camera;
        cb.checked = true; // default: summarize every captioning camera found
        label.appendChild(cb);
        label.appendChild(document.createTextNode(` ${cam.title || cam.camera}`));
        cameraChecks.appendChild(label);
      });
      camerasLoadedForHost = host;
    } catch (err) {
      if (myToken !== camerasRequestToken) return; // superseded -- a newer call already rendered something
      const timedOut = err && err.name === "AbortError";
      cameraChecks.replaceChildren(el("span", "muted",
        timedOut ? `Timed out looking up cameras for ${host}. Click a host again to retry.`
                 : "Couldn't look up cameras for this host."));
      camerasLoadedForHost = null;
    } finally {
      clearTimeout(timeoutId);
    }
  }

  function renderMessage(kind, title, detail) {
    const wrap = el("div", `summary-state summary-state-${kind}`);
    if (kind === "loading") wrap.appendChild(el("div", "spinner"));
    wrap.appendChild(el("div", "summary-state-title", title));
    if (detail) wrap.appendChild(el("div", "summary-state-detail muted", detail));
    body.replaceChildren(wrap);
  }

  function renderReport(report) {
    const wrap = el("div", "summary-report");

    const meta = el("div", "summary-meta muted",
      `${report.host} \u00b7 ${report.cameras.join(", ")} \u00b7 last ${report.window_minutes} min \u00b7 generated ${new Date(report.generated_at).toLocaleString()}`);
    wrap.appendChild(meta);

    wrap.appendChild(el("h3", "summary-section-title", "Overview"));
    wrap.appendChild(el("p", "summary-overview", report.overview));

    if (report.keywords && report.keywords.length) {
      const kwWrap = el("div", "summary-keywords");
      report.keywords.forEach((k) => kwWrap.appendChild(el("span", "summary-keyword", k)));
      wrap.appendChild(kwWrap);
    }

    wrap.appendChild(el("h3", "summary-section-title",
      `Timeline highlights (${report.highlights.length} of ${report.highlights_total_scenes} scenes)`));

    // Grouped by camera -- all of one camera's highlights, chronologically,
    // under its own subheading, then the next camera's -- rather than one
    // feed interleaved by arrival time across cameras (which reads as
    // "camera A, camera B, camera C, camera A, ..." when several cameras
    // are captioning at once, and is hard to follow).
    const groups = (report.highlights_by_camera || []).filter((g) => g.highlights && g.highlights.length);
    if (!groups.length) {
      wrap.appendChild(el("p", "muted", "No scene captions were available for this window."));
    } else {
      groups.forEach((group) => {
        wrap.appendChild(el("h4", "summary-camera-title", group.camera));
        const list = el("ul", "summary-highlights");
        group.highlights.forEach((h) => {
          const item = el("li", "summary-highlight");
          const when = new Date(h.start).toLocaleTimeString();
          const rep = h.occurrences > 1 ? ` (\u00d7${h.occurrences})` : "";
          item.appendChild(el("span", "summary-highlight-meta", `${when}${rep}`));
          item.appendChild(el("span", "summary-highlight-text", h.text));
          list.appendChild(item);
        });
        wrap.appendChild(list);
      });
    }

    const counts = el("div", "summary-device-breakdown muted");
    counts.textContent = Object.entries(report.camera_breakdown)
      .map(([camera, c]) => `${camera}: ${c.captions} caption(s), ${c.scenes} scene(s)`)
      .join(" \u00b7 ");
    wrap.appendChild(counts);

    body.replaceChildren(wrap);
  }

  function pdfUrl(host, cameras, windowMinutes, maxHighlights) {
    const params = new URLSearchParams({
      host,
      cameras: cameras.join(","),
      window_minutes: String(windowMinutes),
      max_highlights: String(maxHighlights),
    });
    return `/api/summary.pdf?${params.toString()}`;
  }

  async function generate() {
    const host = currentHost();
    const cameras = selectedCameras();
    const windowMinutes = Number(windowInput.value) || 15;
    const maxHighlights = Number(highlightsInput.value) || 20;

    downloadBtn.classList.add("hidden");

    if (!host) {
      renderMessage("error", "No UAV host set", "Connect to a UAV companion board first, or enter its IP above.");
      return;
    }
    if (!cameras.length) {
      renderMessage("error", "No camera selected", "Pick at least one camera that is currently captioning.");
      return;
    }

    renderMessage("loading", "Building summary\u2026");
    generateBtn.disabled = true;

    const controller = new AbortController();
    inFlight = controller;
    try {
      const res = await fetch("/api/summary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          host,
          cameras,
          window_minutes: windowMinutes,
          max_highlights: maxHighlights,
        }),
        signal: controller.signal,
      });
      if (!res.ok) {
        const detail = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
      }
      const report = await res.json();
      renderReport(report);
      downloadBtn.href = pdfUrl(host, cameras, windowMinutes, maxHighlights);
      downloadBtn.classList.remove("hidden");
    } catch (err) {
      if (err.name === "AbortError") return;
      renderMessage("error", "Couldn't build the summary",
        "Check that the host is reachable and at least one camera was started with captioning enabled.");
    } finally {
      if (inFlight === controller) inFlight = null;
      generateBtn.disabled = false;
    }
  }

  function open() {
    lastFocus = document.activeElement;
    populateHostSelect();
    modal.classList.remove("hidden");
    renderMessage("idle", "Build a flight summary", "Pick cameras and a time window, then Generate.");
    downloadBtn.classList.add("hidden");
    populateCameras(currentHost());
    (hostSelect.value === CUSTOM_HOST_VALUE ? hostInput : hostSelect).focus();
  }

  function close() {
    if (inFlight) inFlight.abort();
    modal.classList.add("hidden");
    if (lastFocus) lastFocus.focus();
  }

  openBtn.addEventListener("click", open);
  closeBtn.addEventListener("click", close);
  modal.addEventListener("click", (e) => { if (e.target === modal) close(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); generate(); });
  hostInput.addEventListener("change", () => populateCameras(hostInput.value.trim(), { force: true }));
  hostSelect.addEventListener("change", () => {
    hostInput.classList.toggle("hidden", hostSelect.value !== CUSTOM_HOST_VALUE);
    if (hostSelect.value === CUSTOM_HOST_VALUE) {
      hostInput.focus();
    } else {
      populateCameras(hostSelect.value);
    }
  });
  // If the main dashboard's connect/host context changes while this modal is
  // closed, drop the cached camera list so the next open re-fetches instead
  // of showing a stale/empty one for the new host, and keep the host dropdown
  // in sync with whatever tabs are actually open.
  document.addEventListener("uav:connected", () => { camerasLoadedForHost = null; });
  document.addEventListener("uav:tab-switched", () => { camerasLoadedForHost = null; });
  document.addEventListener("uav:sessions-changed", () => {
    if (!modal.classList.contains("hidden")) populateHostSelect();
  });
})();
