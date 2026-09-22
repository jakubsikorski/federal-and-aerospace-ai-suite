#!/usr/bin/env python3
"""Offline UX regression check. Requires Firefox + geckodriver, Python stdlib only.

Run from the repo root: python3 backend/test_ui.py
Optional screenshots: python3 backend/test_ui.py --screenshots /tmp/phase4-ux
Fixtures prove interaction/layout, not live inference or video transport.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen

HTML = Path(__file__).parent / 'app/static/index.html'


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        tracks = [dict(track_id=i, stream_id='cam1', class_label='person' if i % 2 else 'chair',
                       last_observed_utc=now, first_observed_utc=now, candidate_count=1,
                       snip_url='/fixture.svg', snip_state='available', peak_confidence=.87,
                       confidence_freshness='tracker_metadata_unverified') for i in range(1, 41)]
        if self.path == '/':
            payload, kind = HTML.read_bytes(), 'text/html'
        elif self.path == '/fixture.svg':
            payload = b'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="132"><rect width="240" height="132" fill="#394859"/><text x="32" y="72" font-size="20" fill="#f3f1eb">Test evidence</text></svg>'
            kind = 'image/svg+xml'
        elif self.path.startswith('/api/stream') and not self.path.startswith('/api/streams'):
            self.send_response(204)
            self.end_headers()
            return
        else:
            if self.path == '/api/streams':
                data = [dict(stream_id=f'cam{i}', display_name=f'Camera {i}', feed={'ok': i != 3}, whep_path='/whep') for i in range(1, 4)]
            elif self.path == '/api/history/options':
                data = {'classes': ['person', 'chair', 'car'], 'availability': [
                    {'stream_id': 'cam1', 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-86400)), 'ended_utc': now},
                    {'stream_id': 'cam2', 'started_utc': None, 'ended_utc': None}]}
            elif '/current' in self.path:
                data = dict(tracks=tracks, current_presence='present', feed_ok=True)
            elif '/candidates' in self.path:
                data = {'candidates': [dict(candidate_number=1, image_url='/fixture.svg', image_state='available', observed_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-60)))]}
            elif '/tracks?' in self.path:
                from urllib.parse import parse_qs, urlsplit
                label = parse_qs(urlsplit(self.path).query).get('classes', [''])[0]
                chosen = [t for t in tracks if not label or t['class_label'] == label]
                data = dict(tracks=chosen, matched_count=len(chosen), truncated=False, class_counts={'person': 20, 'chair': 20})
            elif self.path == '/api/alerts':
                data = {'alerts': []}
            elif self.path == '/api/models':
                data = {'models': [dict(id=key, label=label, host='Fixture host', service_host=key,
                    configured_model=model, configured_device=device, runtime_model=None, runtime_device=None,
                    checked_utc=now, source='Fixture configuration', note='Configured only.', stages=[], status=status)
                    for key, label, model, device, status in [
                        ('dlsps', 'Vision', 'yolo11s', 'NPU', 'ready'),
                        ('chatqna', 'LLM', 'Intel/neural-chat-7b-v3-3', 'GPU', 'unavailable'),
                        ('audio_analyzer', 'Transcription', 'whisper-medium', 'GPU', 'unavailable'),
                        ('tts', 'Speech', 'microsoft/speecht5_tts', 'GPU', 'unknown')]]}
            elif self.path == '/api/metrics':
                specs = [('cpu_load_percent', 'CPU load', 43, '%'), ('memory_used_percent', 'Memory used', 28, '%'),
                         ('gpu_engine_vcs_percent', 'GPU vcs engine', 22, '%'), ('gpu_engine_ccs_percent', 'GPU ccs engine', 16, '%'),
                         ('gpu_engine_bcs_percent', 'GPU bcs engine', 2, '%'), ('gpu_engine_rcs_percent', 'GPU rcs engine', 0, '%'),
                         ('gpu_engine_vecs_percent', 'GPU vecs engine', 38, '%'), ('npu_utilization_percent', 'NPU utilization', 35, '%'), ('package_power_w', 'Package power', 17.9, 'W'),
                         ('npu_memory_mib', 'NPU memory', None, 'MiB')]
                specs += [(f'diagnostic_{i}', f'Diagnostic {i}', i, 'MHz') for i in range(12)]
                data = dict(status='ok', sampled_utc=now, history_seconds=300, metrics=[
                    dict(id=i, label=label, value=value, unit=unit, state='unavailable' if value is None else 'ok',
                         sampled_utc=now, source='test_fixture', note=None) for i, label, value, unit in specs])
            else:
                data = {}
            payload, kind = json.dumps({'data': data}).encode(), 'application/json'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if self.path != '/api/ask':
            self.send_error(404)
            return
        length = int(self.headers.get('content-length', '0'))
        request = json.loads(self.rfile.read(length) or b'{}')
        now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        page = 1 if request.get('page_token') else 0
        track = dict(track_id=41 + page, stream_id='cam1', class_label='person',
                     last_observed_utc=now, first_observed_utc=now, candidate_count=1,
                     snip_url='/fixture.svg', snip_state='available', peak_confidence=.87,
                     confidence_freshness='tracker_metadata_unverified', intervals=[])
        data = {
            'intent': 'video_history',
            'answer_text': 'Found 2 person tracks in the requested window.',
            'spoken_text': None,
            'errors': [],
            'query': {
                'tracks': [track, dict(track, track_id=999, snip_url=None)], 'total': 4, 'with_crops_total': 2, 'page': page, 'page_size': 2,
                'truncated': page == 0, 'next_page_token': 'fixture-next' if page == 0 else None,
                'window': {'started_utc': now, 'ended_utc': now},
                'filters': {'streams': ['cam1'], 'classes': ['person'],
                            'order': 'last_observed_desc'},
                'warnings': [{'message': 'cam1 has a 12 second feed gap.'}],
            },
        }
        if 'application/x-ndjson' in self.headers.get('accept', ''):
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.end_headers()
            self.wfile.write((json.dumps({'answer': data, 'status': 'Results ready'}) + '\n').encode())
            self.wfile.flush()
            time.sleep(.3)
            self.wfile.write((json.dumps({'answer': data, 'done': True}) + '\n').encode())
            return
        payload = json.dumps({'data': data}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path)
    args = parser.parse_args()
    assert shutil.which('geckodriver'), 'Install Firefox/geckodriver to run the browser check'
    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    # A home-directory profile also works with snap-packaged Firefox.
    with tempfile.TemporaryDirectory(prefix='phase4-ui-', dir=Path.home()) as profile, tempfile.TemporaryFile() as log:
        process = subprocess.Popen(['geckodriver', '--host', '127.0.0.1', '--port', str(port), '--profile-root', profile],
                                   stdout=log, stderr=log, env={**os.environ, 'MOZ_HEADLESS': '1'})
        session = None
        def request(method, path, data=None):
            req = Request(f'http://127.0.0.1:{port}{path}', data=json.dumps(data).encode() if data is not None else None,
                          headers={'Content-Type': 'application/json'}, method=method)
            with urlopen(req, timeout=45) as response:
                return json.load(response)['value']
        try:
            for _ in range(100):
                try:
                    request('GET', '/status')
                    break
                except OSError:
                    time.sleep(.1)
            session = request('POST', '/session', {'capabilities': {'alwaysMatch': {
                'browserName': 'firefox', 'moz:firefoxOptions': {'args': ['-headless']}}}})['sessionId']
            base = f'/session/{session}'
            def js(script):
                return request('POST', base + '/execute/sync', {'script': script, 'args': []})
            def async_js(script):
                return request('POST', base + '/execute/async', {'script': script, 'args': []})
            request('POST', base + '/window/rect', {'width': 1280, 'height': 800})
            request('POST', base + '/url', {'url': f'http://127.0.0.1:{server.server_port}/'})
            for _ in range(100):
                if js("return document.querySelectorAll('.track-card').length") == 40:
                    break
                time.sleep(.1)
            assert js("return document.querySelectorAll('.track-card').length") == 40
            def click(selector):
                element = request('POST', base + '/element', {'using': 'css selector', 'value': selector})
                key = element['element-6066-11e4-a52e-4f735466cecf']
                request('POST', base + '/element/' + key + '/click', {})
            click('#video-fullscreen')
            time.sleep(.5)
            assert js('return document.fullscreenElement?.id') == 'video-wrap'
            assert js('return document.getElementById("video-fullscreen").textContent') == 'Exit fullscreen'
            click('#video-fullscreen')
            time.sleep(.5)
            assert js('return document.fullscreenElement') is None
            assert js('return document.getElementById("video-fullscreen").getAttribute("aria-pressed")') == 'false'
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                if (state.filter.mode !== "current" || $("track-mode").value !== "current") return "wrong default view";
                await openAssistant();
                if ($("search-camera").value !== "all") return "search must default to all cameras";
                if ([...$("search-class").options].map(o => o.value).join(",") !== ",car,chair,person") return "unsorted types";
                if ($("filter-fields").hidden || $("ask-submit").textContent !== "Search") return "wrong initial flow";
                $("search-camera").value = "cam1"; $("search-class").value = "person";
                const button = $("ask-submit").getBoundingClientRect().toJSON();
                await applyAssistantFilters({preventDefault(){}});
                if (state.assistantContext.payload.plan.window.minutes !== 10 || state.assistantContext.payload.plan.classes[0] !== "person") return "wrong filter plan";
                if ($("assistant-results").querySelectorAll(".track-card").length !== 1) return "imageless record shown";
                if (!$("assistant-answer").textContent.includes("4 matching tracks · 2 with images")) return "missing counts";
                await submitAssistant({page_token:state.assistantToken});
                if (!$("assistant-next").disabled || $("assistant-next").hidden || $("assistant-previous").disabled) return "paging moved";
                if ($("ask-submit").getBoundingClientRect().y !== button.y || $("filter-fields").hidden) return "controls moved";
                return true;
              })()`).then(done);
            ''')
            assert result is True, result
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                (args.screenshots / 'search-results.png').write_bytes(base64.b64decode(request('GET', base + '/screenshot')))
            click('#assistant-previous')
            assert js('return document.querySelector("#assistant-results .track-card").dataset.trackKey') == 'cam1:41'
            click('#assistant-results .track-card')
            assert js('return document.getElementById("close-drawer").textContent') == 'Back to results'
            click('#close-drawer')
            assert js('return document.activeElement.dataset.trackKey') == 'cam1:41'
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                $("search-period").value = "session";
                await applyAssistantFilters({preventDefault(){}});
                const row = availableHistory("cam1")[0];
                if (state.assistantContext.payload.plan.window.start_utc !== row.started_utc) return "session range changed";
                $("search-camera").value = "cam2";
                await applyAssistantFilters({preventDefault(){}});
                if (!$("assistant-editor-error").textContent.includes("No records")) return "empty session";
                $("search-camera").value = "cam1";
                $("search-period").value = "custom"; $("search-period").onchange();
                const end = new Date(Date.now()-3600000), start = new Date(end-300000);
                $("search-date").value = localDateValue(start); $("search-end-date").value = localDateValue(end);
                $("search-start").value = start.toTimeString().slice(0,8); $("search-end").value = end.toTimeString().slice(0,8);
                await applyAssistantFilters({preventDefault(){}});
                const window = state.assistantContext.payload.plan.window;
                if (new Date(window.end_utc)-new Date(window.start_utc) !== 300000) return "custom range lost";
                $("ask-text").value = "show cars from 4:20 to 4:25 pm yesterday";
                updateQuestionFilters();
                if (!$("search-class").disabled || !$("search-period").disabled) return "conflicting filters active";
                const before = $("search-start").value;
                await applyAssistantFilters({preventDefault(){}});
                if (state.assistantContext.payload.text !== $("ask-text").value || state.assistantContext.payload.selected_stream !== "cam1") return "question lost";
                renderAssistant({intent:"clarification",answer_text:"Use 24-hour times.", recovery:{reason:"invalid_window"}});
                $("ask-text").value = ""; updateQuestionFilters();
                if ($("search-start").value !== before) return "question overwrote custom time";
                await applyAssistantFilters({preventDefault(){}});
                $("assistant-results").querySelector("img").onerror();
                if ($("assistant-results").querySelector(".track-card") || !$("assistant-results").textContent.includes("No images")) return "failed image retained";
                return true;
              })()`).then(done);
            ''')
            assert result is True, result
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const original = window.fetch;
                const inputs = ["search-camera","search-class","search-period","search-start"].map(id => $(id).value);
                window.fetch = async () => { throw new Error("fixture offline"); };
                try {
                  await applyAssistantFilters({preventDefault(){}});
                  return $("assistant-warnings").textContent.includes("fixture offline")
                    && $("assistant-editor-error").textContent.includes("Press Search to retry")
                    && !$("ask-submit").disabled && !$("filter-fields").hidden
                    && JSON.stringify(inputs) === JSON.stringify(["search-camera","search-class","search-period","search-start"].map(id => $(id).value));
                } finally { window.fetch = original; }
              })()`).then(done);
            ''')
            assert result is True, result
            click('#close-assistant')
            # Execute in the page's global scope, where its lexical state is defined.
            def check(script):
                result = js('return window.eval(' + json.dumps(script) + ')')
                assert result is True, result
            check('''(() => {
              const button = document.querySelector('[data-model="chatqna"]'); button.click();
              const before = button;
              renderModels();
              const stable = !$("model-details").hidden && before === document.querySelector('[data-model="chatqna"]')
                && document.activeElement === $("close-model-details")
                && $("model-device").textContent === "GPU" && $("model-status").textContent === "Unavailable"
                && $("model-note").textContent === "Service offline. Showing assigned model and device.";
              state.modelsUnavailable = true; renderModels();
              const unknown = $("model-status").textContent === "Unknown" && $("model-device").textContent === "GPU";
              state.modelsUnavailable = false;
              const row = state.models.find(item => item.id === "chatqna"), timestamp = row.checked_utc;
              row.checked_utc = "invalid"; renderModels();
              const invalidTime = $("model-status").textContent === "Unknown";
              row.checked_utc = timestamp; closeModelDetails();
              return stable && unknown && invalidTime && document.activeElement === button;
            })()''')
            check('''(() => {
              const first = document.querySelector('[data-model="chatqna"]');
              const second = document.querySelector('[data-model="dlsps"]');
              first.click(); first.click();
              const toggled = $("model-details").hidden && first.getAttribute("aria-expanded") === "false";
              first.click(); second.click();
              const switched = !$("model-details").hidden && state.selectedModel === "dlsps";
              $("model-title").click();
              const panelClosed = $("model-details").hidden && document.activeElement === second;
              first.click(); $("pause-updates").focus(); $("pause-updates").click();
              const outsideClosed = $("model-details").hidden && state.paused
                && document.activeElement === $("pause-updates");
              $("pause-updates").click();
              first.click();
              const range = document.createRange(); range.selectNodeContents($("model-configured"));
              getSelection().removeAllRanges(); getSelection().addRange(range);
              $("model-configured").click();
              const copying = !$("model-details").hidden;
              getSelection().removeAllRanges(); closeModelDetails();
              return toggled && switched && panelClosed && outsideClosed && copying;
            })()''')
            check('''(() => {
              $("track-mode").value = "history";
              $("track-mode").dispatchEvent(new Event("change"));
              state.events.close(); state.events = null;
              window.savedCamera = $("cameras").children[1]; window.savedCard = document.querySelector(".track-card");
              savedCard.focus(); $("track-scroll").scrollTop = 100;
              state.recent.cam1.tracks.reverse(); renderTracks(); renderCameras();
              return state.held && state.pending && document.activeElement === savedCard
                && $("cameras").children[1] === savedCamera && document.querySelector(".track-card") === savedCard
                && $("track-scroll").scrollTop === 100 && !$("apply-updates").disabled;
            })()''')
            check('''(() => {
              $("apply-updates").click();
              return document.querySelector(".track-card").dataset.trackKey === "cam1:40" && !state.pending;
            })()''')
            # Rapid filter changes must not let the older response overwrite the newer selection.
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval('$("class-filter").value = "chair"; changeFilters(); $("class-filter").value = "person"; changeFilters()')
                .then(() => done(window.eval('visibleTracks().length === 20 && visibleTracks().every(t => t.class_label === "person")')));
            ''')
            assert result is True, result
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval('$("class-filter").value = ""; changeFilters()').then(() => done(true));
            ''')
            assert result is True
            # A failed filter fetch must restore all event handlers; stale failures must not reconnect.
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const original = api, previous = state.events;
                let rejectOld;
                api = () => new Promise((resolve, reject) => { rejectOld = reject; });
                const old = changeFilters();
                api = async () => { throw new Error("temporary failure"); };
                await changeFilters();
                const recovered = state.events;
                rejectOld(new Error("stale failure")); await old;
                api = original;
                if (!recovered || recovered === previous || state.events !== recovered) return false;
                recovered.dispatchEvent(new MessageEvent("feed", {data: JSON.stringify({stream_id: "cam1", feed: {ok: false}})}));
                recovered.dispatchEvent(new MessageEvent("alert", {data: JSON.stringify({alerts: []})}));
                const telemetry = structuredClone(state.telemetry); telemetry.status = "recovery-test";
                recovered.dispatchEvent(new MessageEvent("telemetry", {data: JSON.stringify(telemetry)}));
                const recent = structuredClone(state.recent.cam1); recent.matched_count = 123;
                recovered.dispatchEvent(new MessageEvent("track", {data: JSON.stringify({stream_id: "cam1", current: state.current.cam1, recent})}));
                const ok = !state.streams[0].feed.ok && state.alerts.length === 0
                  && state.telemetry.status === "recovery-test" && state.recent.cam1.matched_count === 123;
                await refreshState(); return ok;
              })()` ).then(done);
            ''')
            assert result is True, result
            # Retry unchanged crop URLs, including crops replaced inside an existing card.
            check('''(() => {
              state.paused = false; state.held = false; renderTracks(true);
              const card = document.querySelector(".track-card");
              for (let attempt = 0; attempt < 2; attempt++) {
                const image = card.querySelector("img");
                image.dispatchEvent(new Event("error"));
                if (card.dataset.imageKey || !card.querySelector(".missing")) return false;
                renderTracks();
                if (!card.querySelector("img") || card.querySelector("img") === image) return false;
              }
              return card === document.querySelector(".track-card");
            })()''')
            # Reconnect fetch must preserve All, a paused list, and its DOM nodes.
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval('state.selected = "all"; renderTracks(true); state.paused = true; window.first = document.querySelector(".track-card"); refreshState()')
                .then(() => done(window.eval('state.selected === "all" && state.paused && first === document.querySelector(".track-card")')));
            ''')
            assert result is True, result
            check('''(() => {
              state.paused = false; state.held = false; state.selected = "cam1"; renderTracks(true);
              $("toggle-diagnostics").click(); $("diagnostics").scrollTop = 120;
              const node = $("diagnostics").firstChild, button = $("telemetry-grid").firstChild;
              renderTelemetry();
              return !$("diagnostics").hidden && $("diagnostics").scrollTop === 120
                && node === $("diagnostics").firstChild && button === $("telemetry-grid").firstChild;
            })()''')
            check('''(() => {
              $("toggle-diagnostics").click();
              const now = Date.now(); state.history.set("cpu_load_percent", [
                {at: now - 10000, value: 20}, {at: now - 9000, value: 40},
                {at: now - 2000, value: 50}, {at: now - 1000, value: 45}]);
              renderChart();
              return $("chart").querySelectorAll("polyline").length === 2
                && $("chart").textContent.includes("100%") && $("chart").textContent.includes("5 min ago")
                && $("chart-legend").textContent.includes("CPU load");
            })()''')
            check('''(() => {
              state.telemetry.metrics[0].sampled_utc = new Date(Date.now() - 12000).toISOString(); renderTelemetry();
              return $("telemetry-grid").firstChild.dataset.state === "stale"
                && $("telemetry-status").textContent.includes("Stale")
                && $("diagnostics").textContent.includes("unavailable");
            })()''')
            # Opening evidence protects the image and puts keyboard focus in the drawer.
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval('openDrawer(visibleTracks()[0])').then(() => done(window.eval(`
                (() => { const image = $("large-crop").firstChild; renderTracks();
                  return state.held && document.activeElement === $("close-drawer")
                    && $("track-scroll").inert && image === $("large-crop").firstChild; })()`)));
            ''')
            assert result is True, result
            check('''(() => {
              const rows = [...$("drawer-detail").querySelectorAll("dt")];
              const detail = Object.fromEntries(rows.map(row => [row.textContent, row.nextElementSibling.textContent]));
              const now = new Date(2026, 8, 22, 0, 1, 0), previous = new Date(2026, 8, 21, 23, 59, 0);
              window.clockBeforeInspection = $("operator-clock").dateTime;
              return detail["Image captured"] !== detail["Track last seen"]
                && detail["Image captured"].includes(" · ") && detail["Timezone"]
                && evidenceTime(previous.toISOString(), now) === "Yesterday · 23:59:00"
                && evidenceTime(now.toISOString(), now) === "Today · 00:01:00"
                && evidenceTime(null) === "Unavailable";
            })()''')
            if args.screenshots:
                (args.screenshots / 'image-details.png').write_bytes(base64.b64decode(request('GET', base + '/screenshot')))
            # Thirty real seconds of incoming events while inspecting one image.
            check('''window.inspectedImage = $("large-crop").firstChild; window.inspectedCard = document.querySelector(".track-card"); true''')
            for tick in range(30):
                check('''(() => {
                  const recent = structuredClone(state.recent.cam1);
                  recent.tracks[0].last_observed_utc = new Date().toISOString();
                  state.events.dispatchEvent(new MessageEvent("track", {data: JSON.stringify({stream_id: "cam1", current: state.current.cam1, recent})}));
                  return $("large-crop").firstChild === inspectedImage && document.querySelector(".track-card") === inspectedCard;
                })()''')
                time.sleep(1)
            check('''(() => { closeDrawer(); return $("operator-clock").dateTime !== clockBeforeInspection && !$("track-scroll").inert && document.activeElement.classList.contains("track-card"); })()''')
            check('''(() => {
              state.alerts = [{match_id: "test-alert", matched_utc: new Date().toISOString(), stream_id: "cam2",
                class_label: "person", track_id: 8, rule_name: "Entry", dispatch_state: "delivered"}];
              renderAlerts(); const open = $("alerts").open;
              state.selected = "cam3"; renderCameras(); renderTracks(true);
              const global = $("alert-summary").textContent.includes("Camera 2");
              state.alerts = []; renderAlerts(); $("alerts").open = false;
              state.selected = "cam1"; renderCameras(); $("track-scroll").scrollTop = 0; renderTracks(true);
              return open && global;
            })()''')
            # Palette contrast: primary and secondary text, including selected surfaces.
            check('''(() => {
              const luminance = (hex) => {
                const rgb = hex.match(/[0-9a-f]{2}/gi).map(v => parseInt(v,16)/255)
                  .map(v => v <= .04045 ? v/12.92 : ((v+.055)/1.055)**2.4);
                return rgb[0]*.2126 + rgb[1]*.7152 + rgb[2]*.0722;
              };
              const styles = getComputedStyle(document.documentElement);
              return ["--text", "--muted", "--accent"].every(name => {
                const text = luminance(styles.getPropertyValue(name).trim());
                return ["#20252c", "#292f38", "#35485e"].every(bg => (text+.05)/(luminance(bg)+.05) >= 4.5);
              });
            })()''')
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
            for width, height in [(1280, 714), (1024, 682), (1280, 600), (1024, 540)]:
                request('POST', base + '/window/rect', {'width': width, 'height': height})
                chrome = js('return outerHeight - innerHeight')
                request('POST', base + '/window/rect', {'width': width, 'height': height + chrome})
                time.sleep(.2)
                check('state.resource = "gpu"; renderTelemetry(); true')
                for expanded in (False, True):
                    check(f'''(() => {{
                      if ($("workspace").classList.contains("resources-expanded") !== {str(expanded).lower()}) $("expand-telemetry").click();
                      renderChart();
                      const chart = $("chart").getBoundingClientRect();
                      const panel = $("telemetry").getBoundingClientRect();
                      const controls = [...document.querySelectorAll('#track-controls select, .camera, .model-button')];
                      const header = document.querySelector("header").getBoundingClientRect();
                      const history = $("open-assistant").getBoundingClientRect();
                      const evidenceHead = $("track-title").parentElement.getBoundingClientRect();
                      const ok = header.height <= 64 && history.right <= evidenceHead.right
                        && history.top >= evidenceHead.top && history.bottom <= evidenceHead.bottom
                        && !$("track-count") && $("track-title").textContent === "Evidence"
                        && getComputedStyle($("open-assistant")).backgroundColor !== getComputedStyle($("toggle-diagnostics")).backgroundColor
                        && [...document.querySelectorAll(".quiet-button")].every(button => parseFloat(getComputedStyle(button).borderTopWidth) >= 1 && getComputedStyle(button).height === "32px" && getComputedStyle(button).fontSize === "13px")
                        && parseFloat(getComputedStyle(document.querySelector(".track-seen")).fontSize) >= 15
                        && document.body.scrollWidth === innerWidth && document.body.scrollHeight === innerHeight
                        && $("video-wrap").getBoundingClientRect().height >= 200
                        && $("video-wrap").getBoundingClientRect().bottom <= $("video-panel").getBoundingClientRect().bottom
                        && $("workspace").scrollWidth === $("workspace").clientWidth
                        && chart.height >= 85 && chart.bottom <= panel.bottom
                        && $("chart-note").getBoundingClientRect().bottom <= panel.bottom
                        && $("track-scroll").clientHeight > 100
                        && controls.every((el) => el.getBoundingClientRect().height >= 44);
                      return ok || {{viewport: [innerWidth, innerHeight], expanded: {str(expanded).lower()},
                        boxes: Object.fromEntries(["workspace", "video-panel", "video-wrap", "telemetry", "chart", "chart-note", "track-scroll"]
                          .map(id => [id, $(id).getBoundingClientRect().toJSON()]))}};
                    }})()''')
                check('''(() => {
                  const expanded = $("telemetry").getBoundingClientRect();
                  const video = $("video-panel").getBoundingClientRect();
                  const tracks = $("tracks-panel").getBoundingClientRect();
                  if (!$("video-panel").inert || Math.abs(expanded.top - video.top) > 1
                      || Math.abs(expanded.bottom - tracks.bottom) > 1
                      || $("expand-telemetry").textContent !== "Reduce") return "not fully expanded";
                  $("expand-telemetry").click();
                  const normal = $("telemetry").getBoundingClientRect();
                  const restoredTracks = $("tracks-panel").getBoundingClientRect();
                  const ok = expanded.height > normal.height * 1.5
                    && !$("video-panel").inert && $("expand-telemetry").textContent === "Expand"
                    && tracks.x === restoredTracks.x && tracks.height === restoredTracks.height;
                  return ok || {expanded:expanded.height, normal:normal.height};
                })()''')
                check('''(() => {
                  openAssistant();
                  renderAssistant({answer_text:"Choose an object type and time window."});
                  const intro = $("assistant-panel").getBoundingClientRect();
                  if ($("assistant-paging").getBoundingClientRect().bottom > intro.bottom + 1) return {introOverflow: [innerWidth, innerHeight]};
                  renderAssistant({answer_text:"No matching records.", query:{tracks:[], total:0, with_crops_total:0,
                    page:0, page_size:20, filters:{streams:["cam1"], classes:null},
                    window:{started_utc:new Date().toISOString(), ended_utc:new Date().toISOString()}, warnings:[]}});
                  const panel = $("assistant-panel").getBoundingClientRect();
                  const paging = $("assistant-paging").getBoundingClientRect();
                  const host = $("tracks-panel").getBoundingClientRect();
                  const ok = panel.left >= host.left && panel.right <= host.right + 1
                    && !$("video-panel").inert && !$("telemetry").inert
                    && !$("search-mode") && $("ask-text").getBoundingClientRect().height >= 48
                    && paging.bottom <= panel.bottom + 1
                    && $("assistant-results").clientHeight >= 80
                    && ["search-camera","search-class","search-period"].every(id => $(id).getBoundingClientRect().height >= 48)
                    && ["close-assistant","ask-submit","assistant-previous","assistant-next"].every(id => $(id).getBoundingClientRect().height === 32);
                  closeAssistant(); return ok || {panel:panel.toJSON(), paging:paging.toJSON()};
                })()''')
                if args.screenshots:
                    check('renderChart(); true')
                    data = request('GET', base + '/screenshot')
                    (args.screenshots / f'fixture-{width}x{height}.png').write_bytes(base64.b64decode(data))
            print('PASS: compact in-panel search without mode tabs, alphabetical types, session/custom ranges, question correction, images-only results, fixed Search/paging controls, typed history overlay/paging, stable evidence/focus/scroll, pending updates, reconnect pause/All, diagnostics, chart axes/gaps, stale readings, drawer, native video fullscreen, model placement/detail/failure states, filter failure recovery, repeated crop retry, 200px minimum video at 1280/1024 widths and 714/682/600/540 heights')
        finally:
            if session:
                request('DELETE', f'/session/{session}')
            process.terminate()
            process.wait(timeout=10)
            server.shutdown()


if __name__ == '__main__':
    main()
