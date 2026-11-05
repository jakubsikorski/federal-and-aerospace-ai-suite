// SPDX-FileCopyrightText: (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0

// "Live Natural Language Search": prompt popup + results (player on the left,
// ranked list on the right), backed by viz-backend's /api/search* proxy to the
// separate VSS containers. The entry points stay hidden unless viz-backend
// reports search as enabled (VSS_ENABLED, set by docker-compose-vss.yml).
// All result text is set via textContent.
(() => {
  const modal = document.getElementById("search-modal");
  const openBtns = [document.getElementById("open-search-btn"), document.getElementById("topbar-search-btn")];
  const entryPoints = [document.getElementById("search-panel"), document.getElementById("topbar-search-btn")];
  const closeBtn = document.getElementById("search-modal-close");
  const form = document.getElementById("search-form");
  const input = document.getElementById("search-query-input");
  const submitBtn = document.getElementById("search-submit-btn");
  const vssPill = document.getElementById("search-vss-status");
  const body = document.getElementById("search-body");
  const uavFilterEl = document.getElementById("search-uav-filter");
  const cameraFilterEl = document.getElementById("search-camera-filter");

  const EXAMPLES = ["a white car on the road", "a person walking", "a truck parked near buildings", "an empty street"];

  let inFlight = null;
  let lastFocus = null;
  // Every result the last query returned from the backend, unfiltered --
  // the "UAV"/"Camera" selects narrow this down to `results` (below) purely
  // client-side, rather than re-querying the backend.
  let allResults = [];
  let results = [];
  let filteredOutCount = 0;
  let filter = "all";
  let selectedIndex = -1;
  let playerEl = null;
  let listEl = null;
  let searchAvailable = false;
  let lastQuery = null;
  // {host: [camera, ...]} from the last /api/search/sources fetch, used to
  // rebuild the camera dropdown's options whenever the UAV filter changes.
  let sources = {};
  let sessionLabels = {};

  // ---------- helpers ----------
  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function icon(kind) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    const paths = {
      search: ["M20 20l-3.5-3.5", "circle"],
      alert: ["M12 8v5", "M12 16.5v.01", "M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"],
      film: ["M4 5h16v14H4z", "M8 5v14", "M16 5v14", "M4 9h4", "M4 15h4", "M16 9h4", "M16 15h4"],
    }[kind];
    paths.forEach((d) => {
      if (d === "circle") {
        const c = document.createElementNS(ns, "circle");
        c.setAttribute("cx", "11"); c.setAttribute("cy", "11"); c.setAttribute("r", "7");
        svg.appendChild(c);
      } else {
        const p = document.createElementNS(ns, "path");
        p.setAttribute("d", d);
        svg.appendChild(p);
      }
    });
    return svg;
  }

  function formatTime(seconds) {
    if (seconds == null || Number.isNaN(seconds)) return "?";
    const s = Math.max(0, Math.round(seconds));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  }

  function formatCaptured(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function displayName(r) {
    // Prefer the plain camera name the backend resolved for this result (set
    // whenever it's a live clip from a camera this feature currently
    // manages) over the raw, host-qualified VSS stream_name -- much more
    // readable once multiple UAVs are connected. Deliberately do NOT fall
    // back to the raw stream_name when attribution is unknown: it's a
    // `uav-<host_tag>--<camera>` string, which looks exactly like (and is
    // easily mistaken for) a *different*, still-connected UAV's own camera
    // name/IP -- very misleading for a result that's actually unattributed.
    if (r.source === "live") return r.camera || "Unknown camera";
    return r.filename || r.video_id || "Unknown video";
  }

  // "which camera did this clip come from" -- only meaningful for live
  // results whose stream this feature currently manages; null otherwise.
  function sourceLabel(r) {
    if (r.source !== "live" || !r.camera) return null;
    return r.host ? `${r.camera} \u00b7 ${r.host}` : r.camera;
  }

  function whenText(r) {
    if (r.source === "live" && r.captured_at) return formatCaptured(r.captured_at) || "";
    if (r.segment_start != null && r.segment_end != null && r.segment_end > r.segment_start) {
      return `${formatTime(r.segment_start)} – ${formatTime(r.segment_end)}`;
    }
    return `at ${formatTime(r.seek_timestamp ?? r.segment_start)}`;
  }

  // VSS's relevance score is ranked within this query (the top hit is always
  // 100%). Weak matches are already dropped by the backend using the absolute
  // peak frame similarity, which is shown as a tooltip.
  function scorePct(r) {
    return r.score == null ? null : Math.round(Math.max(0, Math.min(1, r.score)) * 100);
  }

  function scoreTitle(r) {
    const parts = ["Relevance ranked within this search"];
    if (r.peak_score != null) parts.push(`peak frame similarity ${r.peak_score.toFixed(3)}`);
    return parts.join(" · ");
  }

  function setPill(state, text) {
    vssPill.classList.remove("pill-off", "pill-on", "pill-pending");
    vssPill.classList.add(state === "on" ? "pill-on" : state === "off" ? "pill-off" : "pill-pending");
    vssPill.textContent = text;
  }

  // ---------- body states ----------
  function renderMessage(kind, title, detail, withExamples) {
    const wrap = el("div", `search-state search-state-${kind}`);
    if (kind === "loading") {
      wrap.appendChild(el("div", "spinner"));
    } else {
      const ic = el("div", "search-state-icon");
      ic.appendChild(icon(kind === "error" ? "alert" : kind === "empty" ? "film" : "search"));
      wrap.appendChild(ic);
    }
    wrap.appendChild(el("div", "search-state-title", title));
    if (detail) wrap.appendChild(el("div", "search-state-detail muted", detail));
    if (withExamples) {
      const chips = el("div", "search-examples");
      chips.appendChild(el("span", "muted", "Try:"));
      EXAMPLES.forEach((q) => {
        const chip = el("button", "search-example", q);
        chip.type = "button";
        chip.addEventListener("click", () => {
          input.value = q;
          runSearch(q);
        });
        chips.appendChild(chip);
      });
      wrap.appendChild(chips);
    }
    body.replaceChildren(wrap);
    playerEl = null;
    listEl = null;
  }

  function renderIdle() {
    renderMessage("idle", "Search your UAV footage", "Describe a scene, object or activity. Results are ranked by relevance.", true);
  }

  // ---------- player ----------
  // Sized thumbnails and a low-bitrate playback preview keep results usable
  // over slow links; the backend falls back to the originals if it can't
  // produce them.
  const THUMB_WIDTH = 320;
  const POSTER_WIDTH = 960;
  const QUALITY_KEY = "uav-gs.vss-playback-quality";
  const playback = { previewAvailable: false, defaultQuality: "sd", previewHeight: 360 };

  function playbackQuality() {
    if (!playback.previewAvailable) return "hd";
    try {
      const saved = localStorage.getItem(QUALITY_KEY);
      if (saved === "sd" || saved === "hd") return saved;
    } catch (_err) { /* storage blocked: use the default */ }
    return playback.defaultQuality;
  }

  function rememberQuality(q) {
    try { localStorage.setItem(QUALITY_KEY, q); } catch (_err) { /* not persisted */ }
  }

  function videoUrl(r, q) {
    return q === "sd" ? withParam(r.video_proxy_url, "preview", "1") : r.video_proxy_url;
  }

  function withParam(url, key, value) {
    return url ? `${url}${url.includes("?") ? "&" : "?"}${key}=${encodeURIComponent(value)}` : url;
  }

  function renderFrameFallback(container, r, note) {
    const frame = el("div", "player-media player-frame");
    if (r.thumbnail_url) {
      const img = el("img");
      img.alt = "Best-matching frame";
      img.src = withParam(r.thumbnail_url, "width", POSTER_WIDTH);
      img.addEventListener("error", () => {
        img.remove();
        note = "Media is no longer available (it may have expired from VSS live-stream retention).";
        noteEl.textContent = note;
      }, { once: true });
      frame.appendChild(img);
    }
    const noteEl = el("div", "player-note", note);
    frame.appendChild(noteEl);
    container.replaceChildren(frame);
  }

  function renderPlayer(r, autoplay) {
    if (!playerEl) return;
    const mediaBox = el("div", "player-media-box");

    let qualityBar = null;
    if (r.video_proxy_url) {
      const video = el("video", "player-media");
      video.controls = true;
      video.muted = true;
      video.playsInline = true;
      video.preload = "metadata";
      if (r.thumbnail_url) video.poster = withParam(r.thumbnail_url, "width", POSTER_WIDTH);
      const seekTo = r.seek_timestamp ?? r.segment_start ?? 0;
      let quality = playbackQuality();
      let pending = null;
      const syncQualityBar = () => {
        if (!qualityBar) return;
        qualityBar.querySelectorAll(".segmented-btn").forEach((b) => {
          const on = b.dataset.quality === quality;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", String(on));
        });
      };
      // Switching quality reloads the source, so carry over position and play state.
      const load = (q, at, play) => {
        quality = q;
        pending = { at, play };
        video.src = videoUrl(r, q);
        syncQualityBar();
      };
      video.addEventListener("loadedmetadata", () => {
        if (!pending) return;
        const { at, play } = pending;
        pending = null;
        video.currentTime = Math.min(at, video.duration || at);
        if (play) video.play().catch(() => {});
      });
      // If the preview fails, retry with the original clip; if that fails too
      // (browsers can't decode some stored codecs), show the best frame.
      video.addEventListener("error", () => {
        if (quality === "sd") {
          load("hd", pending ? pending.at : video.currentTime || seekTo, pending ? pending.play : !video.paused);
          return;
        }
        if (qualityBar) qualityBar.remove();
        renderFrameFallback(mediaBox, r, "This clip's codec can't be played in the browser — showing the best-matching frame.");
      });

      if (playback.previewAvailable) {
        qualityBar = el("div", "segmented quality-toggle");
        qualityBar.setAttribute("role", "group");
        qualityBar.setAttribute("aria-label", "Playback quality");
        [["sd", "SD", `Fast ${playback.previewHeight}p preview — best on slow connections`],
         ["hd", "HD", "Original full-resolution clip"]].forEach(([q, label, hint]) => {
          const b = el("button", "segmented-btn", label);
          b.type = "button";
          b.dataset.quality = q;
          b.title = hint;
          b.addEventListener("click", () => {
            if (q === quality || !video.isConnected) return;
            rememberQuality(q);
            load(q, video.currentTime, !video.paused);
          });
          qualityBar.appendChild(b);
        });
      }
      load(quality, seekTo, autoplay);
      mediaBox.appendChild(video);
    } else if (r.thumbnail_url) {
      renderFrameFallback(mediaBox, r, "Showing the best-matching frame.");
    } else {
      renderFrameFallback(mediaBox, r, "No preview available for this result.");
    }

    const info = el("div", "player-info");
    const titleRow = el("div", "player-title-row");
    titleRow.appendChild(el("span", `badge ${r.source === "live" ? "badge-live" : "badge-upload"}`, r.source === "live" ? "LIVE" : "UPLOAD"));
    const title = el("h3", "player-title", displayName(r));
    title.title = displayName(r);
    titleRow.appendChild(title);
    const pct = scorePct(r);
    if (pct != null) {
      const pill = el("span", "score-pill", `${pct}% match`);
      pill.title = scoreTitle(r);
      titleRow.appendChild(pill);
    }
    info.appendChild(titleRow);

    const facts = el("dl", "player-facts");
    const addFact = (k, v) => {
      if (!v) return;
      facts.appendChild(el("dt", null, k));
      facts.appendChild(el("dd", null, v));
    };
    if (r.source === "live") addFact("Captured", r.captured_at ? formatCaptured(r.captured_at) : null);
    if (r.source === "live") addFact("Camera", sourceLabel(r));
    if (r.segment_start != null && r.segment_end != null) {
      addFact(r.source === "live" ? "Within segment" : "Clip", `${formatTime(r.segment_start)} – ${formatTime(r.segment_end)}`);
    }
    if (r.seek_timestamp != null) addFact("Best match", formatTime(r.seek_timestamp));
    if (r.tags && r.tags.length) addFact("Tags", r.tags.join(", "));
    info.appendChild(facts);

    const actions = el("div", "player-actions");
    if (qualityBar) actions.appendChild(qualityBar);
    if (r.video_proxy_url && r.seek_timestamp != null) {
      const jump = el("button", "btn btn-ghost btn-sm", "Jump to best match");
      jump.type = "button";
      jump.addEventListener("click", () => {
        const v = mediaBox.querySelector("video");
        if (v) { v.currentTime = r.seek_timestamp; v.play().catch(() => {}); }
      });
      actions.appendChild(jump);
    }
    if (r.video_proxy_url) {
      const dl = el("a", "btn btn-ghost btn-sm", "Download clip");
      dl.href = r.video_proxy_url;
      dl.setAttribute("download", "");
      actions.appendChild(dl);
    }
    if (actions.childElementCount) info.appendChild(actions);

    playerEl.replaceChildren(mediaBox, info);
  }

  // ---------- list ----------
  function visibleResults() {
    return results
      .map((r, i) => ({ r, i }))
      .filter(({ r }) => filter === "all" || r.source === filter);
  }

  function renderListItem(r, i) {
    const item = el("button", "result-item");
    item.type = "button";
    item.dataset.index = String(i);
    item.setAttribute("aria-pressed", "false");

    const thumb = el("div", "result-thumb");
    if (r.thumbnail_url) {
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      img.decoding = "async";
      img.src = withParam(r.thumbnail_url, "width", THUMB_WIDTH);
      img.addEventListener("error", () => { img.remove(); thumb.classList.add("result-thumb-missing"); }, { once: true });
      thumb.appendChild(img);
    } else {
      thumb.classList.add("result-thumb-missing");
    }
    thumb.appendChild(el("span", "result-rank", `#${i + 1}`));
    if (r.seek_timestamp != null) thumb.appendChild(el("span", "result-time", formatTime(r.seek_timestamp)));
    item.appendChild(thumb);

    const text = el("div", "result-text");
    const titleRow = el("div", "result-title-row");
    titleRow.appendChild(el("span", `badge ${r.source === "live" ? "badge-live" : "badge-upload"}`, r.source === "live" ? "LIVE" : "UPLOAD"));
    const title = el("span", "result-title", displayName(r));
    title.title = displayName(r);
    titleRow.appendChild(title);
    text.appendChild(titleRow);
    const whenLine = sourceLabel(r) ? `${whenText(r)} \u00b7 ${sourceLabel(r)}` : whenText(r);
    text.appendChild(el("div", "result-when muted", whenLine));

    const pct = scorePct(r);
    if (pct != null) {
      const score = el("div", "result-score");
      score.title = scoreTitle(r);
      const bar = el("div", "score-bar");
      const fill = el("div", "score-fill");
      fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      score.appendChild(bar);
      score.appendChild(el("span", "score-value", `${pct}%`));
      text.appendChild(score);
    }
    item.appendChild(text);

    item.addEventListener("click", () => select(i, true));
    return item;
  }

  function renderList() {
    if (!listEl) return;
    const items = visibleResults();
    listEl.replaceChildren(...items.map(({ r, i }) => renderListItem(r, i)));
    highlightSelected();
  }

  function highlightSelected() {
    if (!listEl) return;
    listEl.querySelectorAll(".result-item").forEach((node) => {
      const active = Number(node.dataset.index) === selectedIndex;
      node.classList.toggle("active", active);
      node.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  function select(i, autoplay) {
    if (i < 0 || i >= results.length) return;
    selectedIndex = i;
    highlightSelected();
    renderPlayer(results[i], autoplay);
    const node = listEl && listEl.querySelector(`.result-item[data-index="${i}"]`);
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  function renderFilterBar(counts) {
    const bar = el("div", "results-toolbar");
    bar.appendChild(el("span", "results-count", `${results.length} result${results.length === 1 ? "" : "s"}`));
    if (counts.live && counts.upload) {
      const seg = el("div", "segmented");
      [["all", `All ${results.length}`], ["upload", `Uploads ${counts.upload}`], ["live", `Live ${counts.live}`]].forEach(([key, label]) => {
        const b = el("button", `segmented-btn${filter === key ? " active" : ""}`, label);
        b.type = "button";
        b.addEventListener("click", () => {
          filter = key;
          seg.querySelectorAll(".segmented-btn").forEach((n) => n.classList.toggle("active", n === b));
          renderList();
          const visible = visibleResults();
          if (visible.length && !visible.some(({ i }) => i === selectedIndex)) select(visible[0].i, false);
        });
        seg.appendChild(b);
      });
      bar.appendChild(seg);
    }
    return bar;
  }

  function renderResults() {
    const counts = results.reduce((acc, r) => { acc[r.source] = (acc[r.source] || 0) + 1; return acc; }, {});
    filter = "all";

    const layout = el("div", "results-layout");
    playerEl = el("section", "player-pane");
    const listPane = el("section", "list-pane");
    listPane.appendChild(renderFilterBar(counts));
    listEl = el("div", "result-list");
    listEl.setAttribute("role", "list");
    listPane.appendChild(listEl);
    layout.append(playerEl, listPane);
    body.replaceChildren(layout);

    renderList();
    select(0, false);
  }

  // ---------- data ----------
  async function checkVss() {
    setPill("pending", "checking VSS…");
    try {
      const resp = await fetch("/api/search/status");
      const data = await resp.json();
      if (!data.enabled) {
        setPill("off", "search not enabled");
        if (!results.length) {
          renderMessage("error", "Search is not enabled", "Start the ground station with docker-compose-vss.yml to enable search.");
        }
      } else if (!data.reachable) {
        setPill("off", "VSS offline");
        if (!results.length) {
          renderMessage("error", "VSS is not reachable",
            "The VSS search containers are not responding yet. They can take a few minutes to start the first time (models download on first start).");
        }
      } else if (!data.search_enabled) {
        setPill("off", "search disabled");
        if (!results.length) {
          renderMessage("error", "Search is not enabled in VSS", "Redeploy VSS with --search, --dual or --unified.");
        }
      } else {
        setPill("on", "VSS ready");
        if (!results.length && !inFlight && body.querySelector(".search-state-error")) renderIdle();
      }
    } catch (_err) {
      setPill("off", "status unknown");
    }
  }

  function applyFilters() {
    const host = uavFilterEl ? uavFilterEl.value.trim() : "";
    const camera = cameraFilterEl ? cameraFilterEl.value.trim() : "";
    results = allResults.filter((r) => {
      if (host && r.host !== host) return false;
      if (camera && r.camera !== camera) return false;
      return true;
    });
    selectedIndex = -1;
    if (!allResults.length) return; // nothing searched yet -- let runSearch's own messaging stand
    if (!results.length) {
      const detail = host || camera
        ? "No results from this UAV/camera for the current search — try widening or clearing the filter."
        : "Try a different description, or make sure videos have been uploaded and indexed in VSS.";
      renderMessage("empty", `No matches for “${lastQuery}”`, detail, true);
      return;
    }
    renderResults();
  }

  async function runSearch(query) {
    if (inFlight) inFlight.abort();
    const controller = new AbortController();
    inFlight = controller;
    allResults = [];
    results = [];
    selectedIndex = -1;

    submitBtn.disabled = true;
    submitBtn.textContent = "Searching…";
    renderMessage("loading", `Searching for “${query}”…`, "Ranking matching video segments.");
    lastQuery = query;
    try {
      // Always search across every connected UAV's footage -- the "UAV"/
      // "Camera" selects narrow the already-fetched result set client-side
      // rather than being sent to the backend.
      const resp = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query }),
        signal: controller.signal,
      });
      let data = null;
      try { data = await resp.json(); } catch (_e) { /* non-JSON error body */ }
      if (!resp.ok) {
        const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${resp.status}`;
        renderMessage("error", "Search failed", detail);
        return;
      }
      allResults = (data && data.results) || [];
      filteredOutCount = (data && data.filtered_out) || 0;
      if (!allResults.length) {
        const detail = filteredOutCount > 0
          ? "VSS returned some hits, but none were confident enough to show — try a more specific description."
          : "Try a different description, or make sure videos have been uploaded and indexed in VSS.";
        renderMessage("empty", `No matches for “${query}”`, detail, true);
        return;
      }
      applyFilters();
    } catch (err) {
      if (err.name !== "AbortError") renderMessage("error", "Search failed", "Can't reach the ground-station backend.");
    } finally {
      if (inFlight === controller) {
        inFlight = null;
        submitBtn.disabled = false;
        submitBtn.textContent = "Search";
      }
    }
  }

  // ---------- modal ----------
  function isOpen() {
    return !modal.classList.contains("hidden");
  }

  function openModal() {
    if (!searchAvailable) return;
    lastFocus = document.activeElement;
    modal.classList.remove("hidden");
    document.body.classList.add("modal-open");
    if (!body.childElementCount) renderIdle();
    input.focus();
    input.select();
    checkVss();
    populateSourceFilters();
  }

  function closeModal() {
    modal.classList.add("hidden");
    document.body.classList.remove("modal-open");
    body.querySelectorAll("video").forEach((v) => v.pause());
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }

  openBtns.forEach((b) => b && b.addEventListener("click", openModal));

  // Show the search entry points only when this deployment includes VSS.
  const availability = (async () => {
    try {
      const resp = await fetch("/api/search/status");
      if (resp.ok) {
        const data = await resp.json();
        searchAvailable = data.enabled === true;
        const pb = data.playback || {};
        playback.previewAvailable = pb.preview_available === true;
        playback.defaultQuality = pb.default === "hd" ? "hd" : "sd";
        if (Number.isFinite(pb.preview_height)) playback.previewHeight = pb.preview_height;
      }
    } catch (_err) {
      searchAvailable = false;
    }
    entryPoints.forEach((n) => n && n.classList.toggle("hidden", !searchAvailable));
    return searchAvailable;
  })();

  // ---- Search scope filters ("UAV" / "Camera" selects) ----------------
  // Options come from /api/search/sources (uav_naming's registry of
  // currently-managed live streams), not from the dashboard's own list of
  // connected tabs -- a UAV only shows up here once it actually has a
  // running, indexed camera, which is what a search filter should be
  // offering choices over anyway.
  function cameraLabel(cam) {
    return String(cam || "").replace(/^uav-/, "").replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function rebuildCameraOptions() {
    if (!cameraFilterEl) return;
    const selectedHost = uavFilterEl ? uavFilterEl.value : "";
    const previousCamera = cameraFilterEl.value;
    // "All UAVs" selected (or nothing known yet) -- offer the union of every
    // connected UAV's cameras; a specific UAV selected -- only its own.
    const cameraSet = new Set();
    if (selectedHost) {
      (sources[selectedHost] || []).forEach((c) => cameraSet.add(c));
    } else {
      Object.values(sources).forEach((cams) => cams.forEach((c) => cameraSet.add(c)));
    }
    const cameras = Array.from(cameraSet).sort();
    cameraFilterEl.replaceChildren(el("option", null, "All cameras"));
    cameraFilterEl.firstChild.value = "";
    for (const cam of cameras) {
      const opt = el("option", null, cameraLabel(cam));
      opt.value = cam;
      cameraFilterEl.appendChild(opt);
    }
    // Keep the previous selection if it's still offered (e.g. switching from
    // "All UAVs" to one UAV that happens to also have a "front-camera");
    // otherwise fall back to "All cameras" rather than silently keeping a
    // now-invalid filter applied.
    cameraFilterEl.value = cameras.includes(previousCamera) ? previousCamera : "";
  }

  function hostOptionLabel(host) {
    const label = sessionLabels[host];
    return label ? `${label} (${host})` : host;
  }

  async function populateSourceFilters() {
    if (!uavFilterEl || !(await availability)) return;
    try {
      const [sourcesResp, sessionsResp] = await Promise.all([
        fetch("/api/search/sources"),
        fetch("/api/sessions").catch(() => null),
      ]);
      if (!sourcesResp.ok) return;
      const data = await sourcesResp.json();
      const hostList = Array.isArray(data.hosts) ? data.hosts : [];
      sources = Object.fromEntries(hostList.map((h) => [h.host, h.cameras || []]));
      sessionLabels = {};
      if (sessionsResp && sessionsResp.ok) {
        const sessionData = await sessionsResp.json();
        (sessionData.sessions || []).forEach((s) => { if (s.label) sessionLabels[s.host] = s.label; });
      }
    } catch (_err) {
      return; // dropdowns keep their last-known options
    }
    const previousHost = uavFilterEl.value;
    const hosts = Object.keys(sources).sort();
    uavFilterEl.replaceChildren(el("option", null, "All UAVs"));
    uavFilterEl.firstChild.value = "";
    for (const host of hosts) {
      const opt = el("option", null, hostOptionLabel(host));
      opt.value = host;
      uavFilterEl.appendChild(opt);
    }
    uavFilterEl.value = hosts.includes(previousHost) ? previousHost : "";
    rebuildCameraOptions();
  }

  if (uavFilterEl) {
    uavFilterEl.addEventListener("change", () => {
      rebuildCameraOptions();
      applyFilters();
    });
  }
  if (cameraFilterEl) {
    cameraFilterEl.addEventListener("change", () => {
      applyFilters();
    });
  }

  // ---- Live indexing: the backend registers every running UAV pipeline as a
  // VSS live stream; this panel just starts/stops that and shows its state.
  const liveSummary = document.getElementById("live-index-summary");
  const liveList = document.getElementById("live-index-list");
  let livePollTimer = null;
  // Which host's live-sync state to display -- with multiple UAVs connected
  // (one per tab), tracks all of them independently; this panel only ever
  // shows the currently active tab's host, set from the uav:connected event detail.
  let liveSyncHost = null;

  // `data` is the full /api/search/live-sync response: { hosts: {host: {...}}, ... }
  // plus a legacy top-level mirror of one host for callers not yet updated.
  // Pick out `liveSyncHost`'s own state when known; fall back to the legacy
  // mirror (single-UAV deployments, or before any host is known here).
  function stateFor(data) {
    if (data && data.hosts && liveSyncHost && data.hosts[liveSyncHost]) return data.hosts[liveSyncHost];
    return data;
  }

  function renderLiveSync(payload) {
    if (!liveSummary || !liveList) return;
    const data = stateFor(payload);
    const streams = (data && data.streams) || [];
    liveList.replaceChildren(...streams.map((st) => {
      const li = document.createElement("li");
      li.className = "live-index-item";
      const state = String(st.state || "unknown");
      const name = document.createElement("span");
      // Prefer the plain camera name the backend already resolved over the
      // host-qualified stream_name, which is not meant to be read directly.
      name.textContent = String(st.camera || "unknown feed").replace(/^uav-/, "").replace(/-/g, " ").toUpperCase();
      const badge = document.createElement("span");
      badge.className = `live-state live-state-${state.replace(/[^a-z]/g, "")}`;
      badge.textContent = state;
      if (st.last_error) badge.title = st.last_error;
      const count = document.createElement("span");
      count.className = "muted";
      count.textContent = st.embeddings_created != null ? `${st.embeddings_created} indexed` : "";
      li.append(name, count, badge);
      return li;
    }));
    const running = streams.filter((st) => st.state === "running").length;
    if (!data || !data.host) liveSummary.textContent = streams.length ? "paused \u2014 connect to a UAV to resume" : "connect to a UAV to start";
    else if (data.last_error && !streams.length) liveSummary.textContent = "sync error";
    else liveSummary.textContent = `${running} of ${streams.length} feed${streams.length === 1 ? "" : "s"} live`;
    liveSummary.title = (data && data.last_error) || "";
  }

  async function liveSync(method, body) {
    try {
      const resp = await fetch("/api/search/live-sync", {
        method,
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      if (resp.ok) renderLiveSync(await resp.json());
    } catch (_err) { /* panel keeps its last state */ }
  }

  // Search results are never host-scoped by default (a search spans every
  // connected UAV's footage unless the "UAV"/"Camera" filters narrow it
  // deliberately, so switching the active dashboard tab
  // no longer needs to drop/replace whatever's currently shown in the
  // search modal. These listeners only (a) keep the main page's live-index
  // panel showing the *active* tab's own indexing state, and
  // (b) keep the filter dropdowns' options in sync with which UAVs/cameras
  // are actually connected right now.
  document.addEventListener("uav:connected", async (e) => {
    if (!(await availability)) return;
    clearInterval(livePollTimer);
    liveSyncHost = e.detail.host;
    await liveSync("POST", { host: e.detail.host });
    livePollTimer = setInterval(() => liveSync("GET"), 10000);
    if (isOpen()) populateSourceFilters();
  });
  document.addEventListener("uav:disconnected", async (e) => {
    if (!(await availability)) return;
    // Scope the stop to this host only -- with the multi-host-capable
    // backend, omitting it would stop *every* tracked UAV's indexing,
    // not just the one being disconnected here.
    const host = e && e.detail && e.detail.host;
    await liveSync("DELETE", host ? { host } : undefined);
    if (isOpen()) populateSourceFilters();
    if (!host || host === liveSyncHost) {
      clearInterval(livePollTimer);
      livePollTimer = null;
      liveSyncHost = null;
    }
  });
  closeBtn.addEventListener("click", closeModal);
  modal.addEventListener("mousedown", (e) => {
    if (e.target === modal) closeModal();
  });

  document.addEventListener("keydown", (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement && document.activeElement.tagName);
    if (!isOpen()) {
      if (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey && !document.body.classList.contains("modal-open")) {
        e.preventDefault();
        openModal();
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closeModal();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && results.length && document.activeElement !== input) {
      const visible = visibleResults().map(({ i }) => i);
      const pos = visible.indexOf(selectedIndex);
      const next = visible[Math.max(0, Math.min(visible.length - 1, pos + (e.key === "ArrowDown" ? 1 : -1)))];
      if (next != null && next !== selectedIndex) {
        e.preventDefault();
        select(next, true);
        const node = listEl && listEl.querySelector(`.result-item[data-index="${next}"]`);
        if (node) node.focus();
      }
    } else if (e.key === "Tab") {
      // Keep keyboard focus inside the dialog.
      const focusables = [...modal.querySelectorAll("button, a[href], input, video[controls]")].filter((n) => !n.disabled && n.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const query = input.value.trim();
    if (!query) {
      input.focus();
      input.classList.add("input-shake");
      setTimeout(() => input.classList.remove("input-shake"), 400);
      return;
    }
    runSearch(query);
  });
})();
