#!/usr/bin/env python3
"""Offline UX regression check. Requires Firefox + geckodriver, Python stdlib only.

Run from the repo root: python3 backend/test_ui.py
Optional screenshots: python3 backend/test_ui.py --screenshots ui-shots
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


RULES = [dict(rule_id='r1', name='person on any camera', class_label='person', stream_id=None,
              min_confidence=None, enabled=True, cooldown_seconds=60, dwell_seconds=None,
              active_hours=None, zone=None, speak=True)]
RULE_REQUESTS = []
ALERTS = {'alerts': [], 'incidents': []}
CAMERAS = [dict(stream_id='cam1', display_name='Camera 1', enabled=True, origin='config', running=True,
                source={'kind': 'publish', 'publish_url': 'rtsp://127.0.0.1:8554/cam1'}, feed={'ok': True, 'reason': None}),
           dict(stream_id='cam9', display_name='Roof', enabled=True, origin='operator', running=True,
                source={'kind': 'pull', 'url': 'rtsp://192.168.1.9/s'}, feed={'ok': True, 'reason': None})]
CAMERA_REQUESTS = []


def camera_view(**extra):
    enabled = sum(c['enabled'] for c in CAMERAS)
    return dict(cameras=CAMERAS, enabled_count=enabled,
                allowed_sources=['192.168.0.0/16'], **extra)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, data, status=200):
        payload = json.dumps({'data': data}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _camera_mutation(self):
        length = int(self.headers.get('content-length', '0'))
        body = json.loads(self.rfile.read(length) or b'null')
        CAMERA_REQUESTS.append((self.command, self.path, body))
        if self.command == 'POST':
            sid = f'cam{10 + len(CAMERA_REQUESTS)}'
            CAMERAS.append(dict(stream_id=sid, display_name=body['display_name'], enabled=True, origin='operator',
                                running=False, feed={'ok': None, 'reason': 'starting'},
                                source={'kind': 'publish', 'publish_url': f'rtsp://127.0.0.1:8554/{sid}'}))
            return self._json(camera_view(stream_id=sid), 201)
        sid = self.path.split('/')[3].split('?')[0]
        if self.command == 'DELETE':
            CAMERAS[:] = [c for c in CAMERAS if c['stream_id'] != sid]
        else:
            next(c for c in CAMERAS if c['stream_id'] == sid).update(body)
        return self._json(camera_view())

    def _rule_mutation(self):
        if self.path.startswith('/api/cameras'):
            return self._camera_mutation()
        if self.path.startswith('/api/incidents/'):
            RULE_REQUESTS.append((self.command, self.path, None))
            if self.command == 'DELETE':
                return self._json({'incident_id': 'ih', 'removed': True})
            return self._json({'incident_id': 'ih', 'speech_id': 's1', 'text': 'dog on Camera 2.'})
        length = int(self.headers.get('content-length', '0'))
        body = json.loads(self.rfile.read(length) or b'null')
        RULE_REQUESTS.append((self.command, self.path, body))
        if self.command == 'POST':
            rule = dict(rule_id='r2', name=(body['class_label'] or 'Anything detected') + ' on Camera 2', enabled=True, **body)
            RULES.append(rule)
            return self._json(rule, 201)
        rule_id = self.path.split('/')[3].split('?')[0]
        if self.command == 'DELETE':
            RULES[:] = [r for r in RULES if r['rule_id'] != rule_id]
            return self._json({'rule_id': rule_id, 'deleted': True})
        rule = next(r for r in RULES if r['rule_id'] == rule_id)
        rule.update(body)
        return self._json(rule)

    do_PATCH = do_DELETE = _rule_mutation

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
                data = [dict(stream_id=f'cam{i}', display_name=f'Camera {i}', feed={'ok': i != 3}, whep_path='/whep') for i in range(1, 8)]
            elif self.path == '/api/mission/documents':
                data = {
                    'mode': 'ready', 'detail': 'Mission corpus is reconciled.', 'corpus_version': 3,
                    'sources': [dict(filename='mission.pdf', status='ready', in_corpus=True)],
                    'answer_citations_available': False,
                    'citation_notice': 'Source list is an inventory, not an answer citation.',
                }
            elif self.path == '/api/mission/brief':
                data = {
                    'corpus_version': 3, 'stale': False, 'status': 'ready',
                    'brief': {'objective': 'Secure the north entrance.', 'times': 'Not specified'},
                }
            elif self.path == '/api/history/options':
                data = {'classes': ['person', 'chair', 'car'], 'priority_classes': ['person'], 'availability': [
                    {'stream_id': 'cam1', 'started_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-86400)), 'ended_utc': now},
                    {'stream_id': 'cam2', 'started_utc': None, 'ended_utc': None}]}
            elif '/current' in self.path:
                data = dict(tracks=tracks, current_presence='present', feed_ok=True)
            elif '/candidates' in self.path:
                data = {'candidates': [dict(candidate_number=n, image_url='/fixture.svg', image_state='available', observed_at_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time()-60+n))) for n in range(1, 4)]}
            elif '/tracks?' in self.path:
                from urllib.parse import parse_qs, urlsplit
                label = parse_qs(urlsplit(self.path).query).get('classes', [''])[0]
                chosen = [t for t in tracks if not label or t['class_label'] == label]
                data = dict(tracks=chosen, matched_count=len(chosen), truncated=False, class_counts={'person': 20, 'chair': 20})
            elif self.path == '/api/alerts':
                data = ALERTS
            elif self.path == '/api/incidents/ih/tracks':
                now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                data = {'incident_id': 'ih', 'tracks': [
                    {'track_id': n, 'stream_id': 'cam2', 'class_label': 'dog', 'first_observed_utc': now_iso,
                     'last_observed_utc': now_iso, 'matched_utc': now_iso, 'observation_count': 5,
                     'peak_confidence': .9, 'snip_url': None, 'snip_state': 'none', 'candidate_count': 0,
                     'state_source': 'history'} for n in (41, 42, 43)]}
            elif self.path == '/api/cameras':
                data = camera_view()
            elif self.path == '/api/rules':
                data = {'rules': RULES, 'classes': ['person', 'car'], 'streams': [f'cam{i}' for i in range(1, 8)]}
            elif self.path == '/api/models':
                data = {'models': [dict(id=key, label=label, host='Fixture host', service_host=key,
                    configured_model=model, configured_device=device, runtime_model=None, runtime_device=None,
                    checked_utc=now, source='Fixture configuration', note='Configured only.', stages=[], status=status)
                    for key, label, model, device, status in [
                        ('dlsps', 'Vision', 'yolo26s', 'NPU', 'ready'),
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
        if self.path == '/api/cameras':
            return self._camera_mutation()
        if self.path == '/api/rules' or self.path.endswith('/speak'):
            return self._rule_mutation()
        if self.path.startswith('/api/speech/'):
            length = int(self.headers.get('content-length', '0'))
            RULE_REQUESTS.append((self.command, self.path, json.loads(self.rfile.read(length) or b'null')))
            return self._json({'speech_id': self.path.split('/')[3], 'current': True})
        if self.path.startswith('/api/assistant/reset'):
            payload = json.dumps({'data': {'cleared': True, 'turns': 0, 'characters': 0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == '/api/mission/ask':
            payload = json.dumps({'data': {
                'intent': 'mission_knowledge', 'answer_text': 'Mission answer.',
                'answer_citations_available': False,
            }}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
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
    with tempfile.TemporaryDirectory(prefix='handheld-ui-', dir=Path.home()) as profile, tempfile.TemporaryFile() as log:
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
            def click(selector):
                element = request('POST', base + '/element', {'using': 'css selector', 'value': selector})
                key = element['element-6066-11e4-a52e-4f735466cecf']
                request('POST', base + '/element/' + key + '/click', {})
            request('POST', base + '/window/rect', {'width': 1280, 'height': 800})
            request('POST', base + '/url', {'url': f'http://127.0.0.1:{server.server_port}/'})
            for _ in range(100):
                if js("return document.querySelectorAll('.track-card').length") == 40:
                    break
                time.sleep(.1)
            assert js("return document.querySelectorAll('.track-card').length") == 40
            regression = r'''(() => {
              const saved = {recent:{...state.recent}, filter:{...state.filter}, selected:state.selected, priorities:state.priorityClasses};
              state.selected="cam1"; state.filter={mode:"history",classLabel:"",hasImage:false};
              state.priorityClasses=["person"];
              state.recent.cam1={tracks:[
                {track_id:1,class_label:"person",last_observed_utc:"2026-09-23T07:00:00Z"},
                {track_id:2,class_label:"chair",last_observed_utc:"2026-09-23T07:01:00Z"}]};
              const priority = visibleTracks()[0].class_label === "person";
              state.filter.prioritize=false;
              const newest = visibleTracks()[0].class_label === "chair";
              Object.assign(state,{recent:saved.recent,filter:saved.filter,selected:saved.selected,priorityClasses:saved.priorities});
              state.priorityClasses=[];renderClassFilter(); const noPeople=!$("people-only").offsetWidth;
              state.priorityClasses=["car"];renderClassFilter();const cars=$("people-only").textContent === "Car only";
              state.priorityClasses=saved.priorities;renderClassFilter();
              const original=state.telemetry, resource=state.resource;
              const now=new Date().toISOString();
              state.telemetry={metrics:["bcs","ccs","rcs","vcs","vecs"].map((name,i)=>({
                id:"gpu_engine_"+name+"_percent",label:name,value:i*10,unit:"%",state:"ok",sampled_utc:now}))};
              renderTelemetry();const gpu=document.querySelector('[data-resource="gpu"]');
              const maximum=gpu.querySelector("strong").textContent.includes("40% VECS")
                && gpu.querySelector("small").textContent.includes("5 engines");
              state.telemetry.metrics[4].state="stale";renderTelemetry();
              const partial=gpu.querySelector("strong").textContent.includes("30% VCS") && gpu.textContent.includes("Partial");
              state.telemetry.metrics.forEach(m=>m.value=0);renderTelemetry();
              const zero=gpu.querySelector("strong").textContent.includes("0%");
              state.telemetry.metrics.forEach(m=>m.state="stale");renderTelemetry();const stale=gpu.dataset.state==="stale";
              state.telemetry.metrics.forEach(m=>{m.state="unavailable";m.value=null});renderTelemetry();
              const missing=gpu.querySelector("strong").textContent==="Unavailable";
              state.telemetry=original;state.resource=resource;renderTelemetry();
              state.mission={mode:"ready",sources:[{filename:"a.pdf"}],brief:{stale:true,corpus_version:1}};
              $("mission-answer").textContent="Keep my answer";renderMission();
              state.mission.brief={stale:false,corpus_version:1,brief:{objective:"Full answer ".repeat(70)}};renderMission();
              const mission=$("mission-brief-status").textContent.startsWith("Brief ready")
                && $("mission-answer").textContent==="Keep my answer" && $("mission-brief").querySelector("details p").textContent.length>500;
              const turn = document.createElement("article"); turn.className = "chat-turn";
              turn.innerHTML = '<p class="chat-question">q</p><p>a</p>'; $("assistant-transcript").append(turn);
              const size = (el) => getComputedStyle(el).fontSize;
              const fonts = size(turn.lastChild) === size($("assistant-answer")) && size(turn.lastChild) === "15px"
                && size(turn.firstChild) === size($("assistant-submitted")) && size(turn.firstChild) === "14px";
              turn.remove();
              return {priority,newest,noPeople,cars,maximum,partial,zero,stale,missing,mission,fonts};
            })()'''
            checks=js('return window.eval(' + json.dumps(regression) + ')')
            assert all(checks.values()), checks
            click('#mission-toggle')
            for _ in range(100):
                if js("return document.getElementById('mission-status').textContent.includes('Ready')"):
                    break
                time.sleep(.05)
            assert js("return !document.getElementById('mission-panel').hidden")
            assert js("return document.getElementById('mission-sources').textContent.includes('mission.pdf')")
            assert js("return document.getElementById('mission-brief').textContent.includes('Secure the north entrance.')")
            assert js("return document.getElementById('mission-citation-note').textContent.includes('inventory')")
            mission_error_check = '''(() => {
              state.mission.brief = {
                corpus_version: 3, stale: true, status: "ready",
                brief: null, error: "Mission text generation is unavailable."
              };
              renderMission();
              return $("mission-brief-status").textContent.includes("Mission Brief failed")
                && $("mission-brief-status").textContent.includes("Retry Brief / Re-ingest")
                && $("mission-reingest").textContent === "Retry Brief / Re-ingest";
            })()'''
            assert js('return window.eval(' + json.dumps(mission_error_check) + ')')
            click('#close-mission')
            # Rule areas are drawn on the video and tiles without opening Manage alerts first.
            assert js("return window.eval('Array.isArray(state.rules?.rules)')"), 'rules not loaded at startup'
            click('#rules-toggle')
            for _ in range(100):
                if js("return document.getElementById('rule-list').textContent.includes('Person appears in any camera')"):
                    break
                time.sleep(.05)
            assert js("return !document.getElementById('rules-panel').hidden && document.getElementById('mission-panel').hidden")
            assert js("return [...document.getElementById('rule-camera').options].map(o => o.textContent).join('|')") == 'Any camera|Camera 1|Camera 2|Camera 3|Camera 4|Camera 5|Camera 6|Camera 7'
            # Choosing from a select's touch list (a dialog outside the panel) or pressing Escape in it keeps the panel open.
            click('#rule-camera')
            assert js("return document.getElementById('select-picker').open")
            js("document.querySelector('#select-picker-list button').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            assert js("return !document.getElementById('rules-panel').hidden")
            click('#select-picker-list button:nth-child(3)')
            assert js("""return !document.getElementById('select-picker').open && !document.getElementById('rules-panel').hidden
              && document.getElementById('rule-camera').value === 'cam2'"""), js("return [document.getElementById('rules-panel').hidden, document.getElementById('rule-camera').value]")
            js("const s = document.getElementById('rule-camera'); s.value = ''; s.dispatchEvent(new Event('change', {bubbles: true}))")
            assert js("""return window.eval(`(() => {
              if ($('rule-class').value !== '' || $('rule-advanced').open || !$('rule-duration').hidden) return false;
              document.querySelector('input[name=rule-kind][value=dwell]').click();
              if ($('rule-duration').hidden || ruleDwell() !== 30 || !$('rule-custom-duration').hidden) return false;
              $('rule-duration-choice').value='60'; updateRuleForm();
              if(ruleDwell() !== 60 || !$('rule-preview').textContent.includes('60 seconds')) return false;
              document.querySelector('input[name=rule-kind][value=appears]').click();
              return ruleDwell() === null && $('rule-duration').hidden;
            })()` )"""), 'Simple alert defaults or duration controls failed'
            js("document.getElementById('rule-class').value = 'person'; document.getElementById('rule-camera').value = 'cam2'; document.getElementById('rule-confidence').value = '0.6'")
            click('#rule-form button[type="submit"]')
            for _ in range(100):
                if js("return document.getElementById('rule-list').children.length") == 2:
                    break
                time.sleep(.05)
            assert RULE_REQUESTS[-1] == ('POST', '/api/rules', {'class_label': 'person', 'stream_id': 'cam2',
                'min_confidence': 0.6, 'cooldown_seconds': 30, 'dwell_seconds': None, 'active_hours': None, 'zone': None, 'speak': True}), RULE_REQUESTS
            click('#rule-list li:first-child button:nth-of-type(2)')
            for _ in range(100):
                if RULE_REQUESTS[-1][0] == 'PATCH':
                    break
                time.sleep(.05)
            assert RULE_REQUESTS[-1] == ('PATCH', '/api/rules/r1', {'enabled': False}), RULE_REQUESTS
            # Edit reuses the form, PATCHes the same rule and regenerates an automatic name.
            click('#rule-list li:first-child button:nth-of-type(1)')
            assert js("""return document.getElementById('rule-submit').textContent === 'Save changes'
              && !document.getElementById('rule-cancel-edit').hidden
              && document.querySelector('#rule-list li:first-child').hasAttribute('data-editing')
              && document.getElementById('rule-camera').value === ''"""), 'Edit did not load the rule'
            js("document.getElementById('rule-camera').value = 'cam3'; document.getElementById('rule-confidence').value = '0.7'")
            click('#rule-submit')
            for _ in range(100):
                if RULE_REQUESTS[-1][1] == '/api/rules/r1' and 'name' in (RULE_REQUESTS[-1][2] or {}):
                    break
                time.sleep(.05)
            assert RULE_REQUESTS[-1] == ('PATCH', '/api/rules/r1', {'class_label': 'person', 'stream_id': 'cam3',
                'min_confidence': 0.7, 'cooldown_seconds': 60, 'dwell_seconds': None, 'active_hours': None, 'zone': None, 'speak': True, 'name': 'person on Camera 3'}), RULE_REQUESTS[-1]
            for _ in range(100):
                if js("return document.getElementById('rule-submit').textContent") == 'Create alert':
                    break
                time.sleep(.05)
            assert js("return document.getElementById('rule-cancel-edit').hidden && document.getElementById('rule-list').textContent.includes('Person appears in Camera 3')")
            click('#rule-list li:first-child button:nth-of-type(1)')
            click('#rule-cancel-edit')
            assert js("return document.getElementById('rule-submit').textContent === 'Create alert' && !document.querySelector('#rule-list [data-editing]')")
            js("""window.eval(`(() => {
              document.querySelector('input[name=rule-kind][value=dwell]').checked=true; $('rule-duration-choice').value='custom'; updateRuleForm(); $('rule-camera').value = 'cam2'; $('rule-dwell').value = '45'; $('rule-active-start').value = '22:00'; $('rule-active-end').value = '06:00';
              state.zoneDraft = {points:[[0.1,0.2],[0.8,0.2],[0.7,0.75]]}; updateZoneSummary();
            })()` )""")
            click('#rule-submit')
            for _ in range(100):
                if RULE_REQUESTS[-1][0] == 'POST' and RULE_REQUESTS[-1][2].get('dwell_seconds') == 45:
                    break
                time.sleep(.05)
            rule_payload = RULE_REQUESTS[-1][2]
            assert rule_payload['class_label'] is None and rule_payload['dwell_seconds'] == 45 and rule_payload['active_hours']['start'] == '22:00' \
                and rule_payload['active_hours']['end'] == '06:00' and rule_payload['active_hours']['timezone'] \
                and rule_payload['zone']['points'] == [[0.1, 0.2], [0.8, 0.2], [0.7, 0.75]], rule_payload
            fixed_rules = async_js("""const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const mutate = ruleMutation;
                let submitted;
                try {
                  editRule({...state.rules.rules[0], stream_id:'cam2',
                    active_hours:{start:'08:00',end:'17:00',timezone:'Pacific/Honolulu'},
                    zone:{points:[[.1,.1],[.8,.1],[.8,.8]]}});
                  state.zoneBackup=structuredClone(state.zoneDraft);
                  $('zone-editor').hidden=false; drawZoneDraft();
                  $('zone-clear').click();
                  if ($('zone-done').disabled || $('zone-done').textContent !== 'Remove area') return 'cannot remove zone';
                  $('zone-done').click();
                  if (!$('zone-editor').hidden || state.zoneDraft.points.length) return 'zone removal not saved';
                  ruleMutation=async (path,method,fields)=>{submitted=fields;return false;};
                  await $('rule-form').onsubmit({preventDefault(){}});
                  if(submitted.zone !== null || submitted.active_hours.timezone !== 'Pacific/Honolulu') return submitted;
                  if(getComputedStyle($('alert-list')).maxHeight !== '150px') return 'alert height budget changed';
                  const savedStreams=state.streams, savedPage=state.gridPage;
                  try {
                    state.streams=state.streams.slice(0,3);state.gridPage=0;renderCameraGrid();
                    if (!$('grid-pager').hidden) return 'single page reserves pager space';
                    state.streams=Array.from({length:5},(_,i)=>({...savedStreams[0],stream_id:'check'+i}));
                    renderCameraGrid();
                    if ($('grid-pager').hidden) return 'multiple pages cannot be reached';
                  } finally {state.streams=savedStreams;state.gridPage=savedPage;renderCameraGrid();}
                  return true;
                } finally {ruleMutation=mutate;stopEditingRule();}
              })()`).then(done).catch(error=>done(String(error)));
            """)
            assert fixed_rules is True, fixed_rules
            js("const c = document.getElementById('rule-camera'); c.value = 'cam2'; c.dispatchEvent(new Event('change')); document.getElementById('rule-draw-zone').click()")
            assert js("return document.getElementById('rules-status').textContent.includes('Waiting for a frame from Camera 2')")
            time.sleep(20.6)
            assert js("return document.getElementById('rules-status').textContent.includes('Camera 2 has not sent a video frame') && document.getElementById('zone-editor').hidden")
            click('#close-rules')
            # The operator's unsaved camera choice survives closing Rules and switching the live camera.
            js("window.eval('selectCamera(\"cam1\")')")
            click('#rules-toggle')
            assert js("return document.getElementById('rule-camera').value") == 'cam2', 'unsaved rule camera reset on reopen'
            js("window.eval('state.ruleCameraPicked = false')")
            # Light dismiss: a click elsewhere closes Rules, Escape closes Mission from anywhere.
            assert js("return !document.getElementById('rules-panel').hidden")
            js("document.getElementById('track-title').dispatchEvent(new PointerEvent('pointerdown', {bubbles: true}))")
            assert js("return document.getElementById('rules-panel').hidden"), 'outside click left Rules open'
            click('#mission-toggle')
            js("document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
            assert js("return document.getElementById('mission-panel').hidden"), 'Escape left Mission open'
            # A live update must not rebuild (and so close) a More menu the operator has open.
            menu_hold = js('''return window.eval(`(() => {
              const now = new Date().toISOString();
              state.announced = new Set(["h1"]);
              const incident = {incident_id: "ih", head_match_id: "h1", state: "open", stream_id: "cam2", class_label: "dog",
                rule_id: "r1", rule_name: "dog", rule_enabled: true, match_count: 1, opened_utc: now, last_match_utc: now,
                dispatch_state: "pending", snip_url: null, track: {track_id: 1}};
              state.incidents = [incident]; renderAlerts(); $("alerts").open = true;
              const menu = $("alert-list").querySelector("select"); menu.focus();
              state.incidents = [{...incident, track: {track_id: 2}}]; renderAlerts();
              const ignoresTrack = !$("alert-list").dataset.deferred;
              state.incidents = [{...incident, match_count: 2}]; renderAlerts();
              window.heldMenu = menu;
              return ignoresTrack && menu.isConnected && document.activeElement === menu && $("alert-list").dataset.deferred === "1";
            })()`)''')
            assert menu_hold, 'incident update closed the open More menu'
            js("window.heldMenu.blur()")
            time.sleep(.2)
            assert js("return !window.heldMenu.isConnected && document.getElementById('alert-list').textContent.includes('2 sightings')"), 'deferred incident update never applied'
            # View shows every object in the incident in the Evidence panel itself, not a popup.
            js("document.getElementById('alert-list').querySelector('button').click()")
            grid = "document.querySelector('#track-scroll .track-grid')"
            for _ in range(100):
                if js(f"return {grid}.querySelectorAll('.track-card').length") == 3:
                    break
                time.sleep(.05)
            assert js(f"""return document.getElementById('tracks-panel').classList.contains('alert-view')
              && document.getElementById('alert-view-copy').textContent.includes('Dog · Camera 2')
              && document.getElementById('scope-copy').textContent.includes('3 objects')
              && getComputedStyle(document.getElementById('track-controls')).display === 'none'
              && {grid}.querySelector('.track-card').dataset.trackKey.endsWith(':41')"""), 'View did not show the sightings as Evidence'
            click('#track-scroll .track-card:nth-child(2)')
            assert js("return document.getElementById('drawer-title').textContent.includes('#42')")
            js("document.getElementById('close-drawer').focus(); document.getElementById('drawer').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}))")
            assert js("return !document.getElementById('drawer').classList.contains('open') && document.getElementById('tracks-panel').classList.contains('alert-view')"), 'drawer Back left the alert view'
            js("document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}))")
            assert js("return !document.getElementById('tracks-panel').classList.contains('alert-view')"), 'Escape did not return to live evidence'
            js("document.getElementById('alert-list').querySelector('button').click()")
            time.sleep(.3)
            click('#cameras .camera:nth-child(2)')
            assert js("return !document.getElementById('tracks-panel').classList.contains('alert-view')"), 'camera tab did not leave the alert view'
            js("window.eval('state.incidents = []; renderAlerts(); $(\"alert-toast\").hidden = true; selectCamera(\"cam1\")')")
            time.sleep(.5)
            alert_check = '''(() => {
              const saved = state.incidents;
              const now = new Date().toISOString();
              const base = {rule_id: "r1", rule_name: "person on any camera", stream_id: "cam2", class_label: "person",
                opened_utc: now, last_match_utc: now, match_count: 1, rule_enabled: true, attempts: 1, last_error: null,
                snip_url: null, track: null};
              state.announced = null;
              state.incidents = [
                {...base, incident_id: "i1", head_match_id: "m1", state: "open", match_count: 4,
                 dispatch_state: "failed", attempts: 4, undelivered: true, speech: "text_only", last_error: "ConnectError"},
                {...base, incident_id: "i2", head_match_id: "m2", state: "snoozed", stream_id: "cam1",
                 snoozed_until_utc: now, dispatch_state: "delivered", speech: "spoken"},
                {...base, incident_id: "i3", head_match_id: "m3", state: "closed", closure: "false_alarm",
                 dispatch_state: "delivered"}];
              renderAlerts();
              const rows = [...$("alert-list").children].map(li => li.textContent);
              const buttons = [...$("alert-list").children].map(li => [...li.querySelectorAll("button")].map(b => b.textContent));
              const menus = [...$("alert-list").children].map(li => [...li.querySelectorAll("option")].map(o => o.value));
              const badge = document.querySelector('.camera[data-stream="cam2"]').dataset.alerts;
              const noBadge = document.querySelector('.camera[data-stream="cam1"]').dataset.alerts === undefined;
              const summary = $("alert-summary").textContent;
              const firstToast = $("alert-toast").hidden;
              const wasOpen = $("alerts").open; $("alerts").open = false;
              state.incidents = [{...state.incidents[0], head_match_id: "m9", match_count: 5}, ...state.incidents.slice(1)];
              renderAlerts();
              const shown = !$("alert-toast").hidden && $("alert-toast").textContent.includes("Person · Camera 2");
              // The banner stays until the alert is handled, also with the tray open.
              renderAlerts();
              const persists = !$("alert-toast").hidden;
              $("alerts").open = true;
              state.incidents = [{...state.incidents[0], head_match_id: "m10"}, ...state.incidents.slice(1)];
              renderAlerts();
              const withTray = !$("alert-toast").hidden;
              // The alerting rule's zone is highlighted on its camera.
              const savedRules = state.rules, savedSelected = state.selected;
              state.rules = {...(savedRules || {}), rules: [{rule_id: "r1", stream_id: "cam2", enabled: true, zone: {points: [[.1, .1], [.5, .1], [.3, .5]]}}]};
              state.selected = "cam2"; renderZoneOverlay();
              const flashed = !!$("zone-overlay").querySelector("polygon.alerting") && $("video-wrap").dataset.alert === "true";
              state.rules = savedRules; state.selected = savedSelected; renderZoneOverlay();
              state.incidents = [{...state.incidents[0], state: "acknowledged"}, ...state.incidents.slice(1)];
              renderAlerts();
              const toast = shown && persists && withTray && flashed && $("alert-toast").hidden;
              $("alerts").open = wasOpen;
              state.speech = {current: null, talk_held: false, queued: [], last: {outcome: "text_only", text: "person on Camera 2."}};
              renderSpeech();
              const textOnly = $("speech-line").textContent.startsWith("Text only");
              state.speech = {current: null, talk_held: true, queued: [{id: "x"}], last: null};
              renderSpeech();
              const held = $("speech-line").textContent.includes("held while you talk");
              state.speech = {current: null, talk_held: false, queued: [], last: null};
              renderSpeech();
              // After Acknowledge the span restarts at the first new sighting; unheard speech is plain.
              state.audioOn = false;
              state.incidents = [{...base, incident_id: "i4", head_match_id: "m4", state: "open", opened_utc: "2026-09-24T08:00:00Z",
                counted_from_utc: now, dispatch_state: "delivered", speech: "unconfirmed"}];
              renderAlerts();
              const restarted = !$("alert-list").textContent.includes(shortTime("2026-09-24T08:00:00Z"))
                && $("alert-list").textContent.includes(shortTime(now)) && $("alert-list").textContent.includes("not played (Audio off)")
                && !$("alert-list").textContent.includes("confirmed");
              state.incidents = saved; renderAlerts();
              return {rows, buttons, menus, badge, summary, textOnly, held, restarted,
                retry: menus[0].includes("retry") && !menus[1].includes("retry") && menus[0].includes("disable"),
                ok: rows[0].includes("Person · Camera 2") && rows[0].includes("Needs attention") && rows[0].includes("4 sightings")
                  && rows[0].includes("Undelivered") && rows[0].includes("text only")
                  && rows[1].includes("Snoozed") && rows[2].includes("false alarm")
                  && buttons[0].slice(1).join() === "Acknowledge,View,Speak" && buttons[1].join().endsWith("View,Speak")
                  && !buttons.flat().includes("Remove") && menus.every(m => m[m.length - 1] === "remove") && menus[2].join() === ",remove"
                  && badge === "1" && noBadge && summary.includes("1 needs attention") && firstToast && toast};
            })()'''
            alerts = js('return window.eval(' + json.dumps(alert_check) + ')')
            assert alerts['ok'] and alerts['retry'] and alerts['textOnly'] and alerts['held'] and alerts['restarted'], alerts
            # Test controls: Speak asks the Backend to speak the alert; Remove deletes it after confirm.
            before = len(RULE_REQUESTS)
            js("""window.eval(`(() => {
              const now = new Date().toISOString();
              state.incidents = [{incident_id: "ih", head_match_id: "h1", state: "closed", closure: "dismissed", stream_id: "cam2",
                class_label: "dog", rule_id: "r1", rule_name: "dog", rule_enabled: true, match_count: 1, opened_utc: now,
                last_match_utc: now, dispatch_state: "delivered", snip_url: null, track: null}];
              renderAlerts(); window.confirm = () => true;
              const speak = [...$("alert-list").querySelectorAll("button")].find(b => b.textContent === "Speak");
              const more = $("alert-list").querySelector("select");
              speak.click(); more.value = "remove"; more.dispatchEvent(new Event("change"));
            })()`)""")
            time.sleep(.5)
            assert sorted(RULE_REQUESTS[before:]) == [('DELETE', '/api/incidents/ih', None), ('POST', '/api/incidents/ih/speak', None)], RULE_REQUESTS[before:]
            js("window.eval('state.incidents = []; renderAlerts()')")
            # Older alerts stay reachable: the list collapses to 8 with Show all, and says when the server capped it.
            paging = js('''return window.eval(`(() => {
              const now = new Date().toISOString();
              state.incidents = Array.from({length: 12}, (_, n) => ({incident_id: "p" + n, head_match_id: "pm" + n, state: "closed",
                closure: "dismissed", stream_id: "cam2", class_label: "person", rule_id: "r1", rule_name: "p", rule_enabled: true,
                match_count: 1, opened_utc: now, last_match_utc: now, dispatch_state: "delivered", snip_url: null, track: null}));
              state.incidentCounts = {open: 0, active: 0, total: 12}; state.alertsExpanded = false; renderAlerts();
              const collapsed = $("alert-list").children.length === 8 && !$("alert-page").hidden && $("alert-more").textContent === "Show all 12";
              $("alert-more").click();
              const expanded = $("alert-list").children.length === 12 && $("alert-more").textContent === "Show fewer"
                && $("alert-more").getAttribute("aria-expanded") === "true" && $("alert-page-note").textContent === "";
              state.incidentCounts = {open: 3, active: 5, total: 60}; renderAlerts();
              const capped = $("alert-page-note").textContent.includes("Newest 12 of 60") && $("alert-summary").textContent.includes("3 need attention");
              $("alert-more").click();
              state.incidents = []; state.incidentCounts = null; renderAlerts();
              return {collapsed, expanded, capped};
            })()`)''')
            assert all(paging.values()), paging
            # A failed action is shown inline and the list is rebuilt, so its button works again.
            now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            ALERTS['incidents'] = [dict(incident_id='ie', head_match_id='he', state='open', stream_id='cam2', class_label='dog',
                rule_id='r1', rule_name='dog', rule_enabled=True, match_count=1, opened_utc=now_iso, last_match_utc=now_iso,
                dispatch_state='delivered', snip_url=None, track=None)]
            js("window.eval('refreshIncidents()')")
            for _ in range(100):
                if js("return [...document.querySelectorAll('#alert-list button')].some(b => b.textContent === 'Acknowledge')"):
                    break
                time.sleep(.05)
            js("[...document.querySelectorAll('#alert-list button')].find(b => b.textContent === 'Acknowledge').click()")
            for _ in range(100):
                if not js("return document.getElementById('alert-error').hidden"):
                    break
                time.sleep(.05)
            assert js("""return document.getElementById('alert-error').textContent.startsWith('Acknowledge failed')
              && [...document.querySelectorAll('#alert-list button')].find(b => b.textContent === 'Acknowledge').disabled === false"""), 'failed action left no error or a dead button'
            ALERTS['incidents'] = []
            js("window.eval('refreshIncidents(); showAlertError(\"\")')")
            # Speech counts as spoken only when this tab plays it; Audio on restarts the current utterance.
            before = len(RULE_REQUESTS)
            audio = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const RealAudio = window.Audio, clips = [];
                let block = false;
                window.Audio = class { constructor(src) { this.src = src; clips.push(this); }
                  play() { if (block) return Promise.reject(Object.assign(new Error("no"), {name: "NotAllowedError"}));
                    setTimeout(() => this.onplaying?.(), 0); return Promise.resolve(); }
                  pause() {} };
                const wait = () => new Promise(r => setTimeout(r, 50));
                state.audioOn = false; renderAudioToggle();
                state.speech = {current: {id: "sp1", text: "dog on Camera 2.", audio_base64: "AAAA"}, queued: [], talk_held: false, last: null};
                renderSpeech();
                const off = $("speech-line").textContent.startsWith("Audio off") && clips.length === 0;
                $("audio-toggle").click(); await wait();
                const speaking = $("speech-line").textContent.startsWith("Speaking:") && clips.length === 2 && state.playingId === "sp1";
                clips[1].onended(); await wait();
                stopPlayback(); state.endedId = null; block = true; state.playback = null;
                renderSpeech(); await wait();
                const blocked = !state.audioOn && $("audio-toggle").textContent === "Audio off"
                  && $("speech-line").textContent.includes("browser blocked audio");
                state.speech = {current: null, queued: [], talk_held: false, last: null};
                renderSpeech(); window.Audio = RealAudio;
                return {off, speaking, blocked, stopped: state.audio === null};
              })()`).then(done, e => done(String(e)))''')
            assert isinstance(audio, dict) and all(audio.values()), audio
            acks = [(p, b) for _, p, b in RULE_REQUESTS[before:]]
            assert acks == [('/api/speech/sp1/playback', {'event': 'started'}), ('/api/speech/sp1/playback', {'event': 'ended'}),
                            ('/api/speech/sp1/playback', {'event': 'error'})], acks
            # Another tab ending a chunk first must not cut this tab: it plays out,
            # then the preloaded next chunk starts at once.
            chain = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const RealAudio = window.Audio, clips = [];
                window.Audio = class { constructor(src) { this.src = src; this.paused = false; clips.push(this); }
                  play() { setTimeout(() => this.onplaying?.(), 0); return Promise.resolve(); }
                  pause() { this.paused = true; } };
                const wait = () => new Promise(r => setTimeout(r, 50));
                state.audioOn = true; state.endedId = null; stopPlayback();
                const a = {id: "c1", kind: "answer", text: "First part.", audio_base64: "AAAA"};
                const b = {id: "c2", kind: "answer", text: "Second part.", audio_base64: "BBBB"};
                state.speech = {current: a, next: b, queued: [], talk_held: false, last: null};
                renderSpeech(); await wait();
                const preloaded = state.nextUp?.id === "c2";
                state.speech = {current: b, queued: [], talk_held: false, last: {id: "c1", kind: "answer", outcome: "spoken"}};
                renderSpeech(); await wait();
                const notCut = state.playingId === "c1" && !clips[0].paused && $("speech-line").textContent === ""
                  && $("assistant-speech-line").textContent === "Speaking: First part."
                  && $("assistant-speech-line").querySelector(".speech-label")?.textContent === "Speaking:";
                clips[0].onended(); await wait();
                const next = state.playingId === "c2" && state.audio === clips[1];
                state.speech = {current: null, queued: [], talk_held: false, last: {id: "c2", kind: "answer", outcome: "stopped"}};
                renderSpeech(); await wait();
                const stopped = state.audio === null && clips[1].paused;
                window.Audio = RealAudio;
                return {preloaded, notCut, next, stopped};
              })()`).then(done, e => done(String(e)))''')
            assert isinstance(chain, dict) and all(chain.values()), chain
            click('#video-fullscreen')
            time.sleep(.5)
            assert js('return document.fullscreenElement?.id') == 'video-wrap'
            assert js('return document.getElementById("video-fullscreen").textContent') == 'Exit fullscreen'
            click('#video-fullscreen')
            time.sleep(.5)
            assert js('return document.fullscreenElement') is None
            assert js('return document.getElementById("video-fullscreen").getAttribute("aria-pressed")') == 'false'
            new_ui = async_js(r'''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const now = new Date().toISOString();
                state.announced = new Set(["opt"]);
                state.incidents = [{incident_id:"opt", head_match_id:"opt", state:"open", stream_id:"cam2", class_label:"person", rule_id:"r1", rule_name:"long status rule", rule_enabled:true, match_count:2, opened_utc:now, last_match_utc:now, dispatch_state:"delivered", speech:"unconfirmed", snip_url:null, track:null}];
                renderCameras(); renderAlerts(); $("alerts").open = true;
                const row = $("alert-list").querySelector("li"), main = row.querySelector(".incident-main"), ack = row.querySelector(".ack-button"), meta = row.querySelector(".incident-meta");
                if (!main || main.tagName !== "BUTTON" || !main.getAttribute("aria-label").includes("View alert")) return "alert main is not accessible button";
                const view = [...row.querySelectorAll('.incident-actions button')].find(b => b.textContent === "View");
                if (row.querySelector('.incident-actions').firstElementChild !== ack || Math.abs(ack.getBoundingClientRect().height - view.getBoundingClientRect().height) > 1
                    || getComputedStyle(ack).borderTopColor === getComputedStyle(view).borderTopColor) return "ack must be first, same size, distinct border";
                if (getComputedStyle(meta).whiteSpace === "nowrap" || main.getBoundingClientRect().width < 220) return "alert text target too narrow";
                const actions = row.querySelector('.incident-actions').getBoundingClientRect(), mainBox = main.getBoundingClientRect();
                if (innerWidth >= 1280 && (actions.top >= mainBox.bottom - 1 || main.scrollHeight > main.clientHeight + 1)) return "alert row wraps or overflows: " + JSON.stringify([actions.top, mainBox.bottom, main.scrollHeight, main.clientHeight]);
                if (document.querySelector("header").getBoundingClientRect().height > 64 || !document.querySelector('.camera[data-alerts]')) return "alert badge grows header";
                main.click(); await new Promise(r=>setTimeout(r,80));
                if (state.selected !== "cam2" || !$('tracks-panel').classList.contains('alert-view')) return "alert main did not view incident";
                exitAlertView(false);
                let releaseReject;
                const realRefreshIncidents = refreshIncidents; refreshIncidents = async () => {};
                const pending = alertAction("Remove", () => new Promise((_, reject) => { releaseReject = reject; }), () => { state.incidents = []; });
                if (state.incidents.length !== 0) return "remove did not apply optimistically";
                releaseReject(new Error("offline")); await pending; refreshIncidents = realRefreshIncidents;
                if (state.incidents.length !== 1 || !$('alert-error').textContent.startsWith('Remove failed')) return "optimistic rollback failed";
                selectCamera("all"); await new Promise(r=>setTimeout(r,50));
                if ($('camera-grid').children.length !== 4 || !$('grid-page-label').textContent.includes('1–4 of 7')) return "grid first page missing";
                $('grid-next').click(); await new Promise(r=>setTimeout(r,50));
                if ($('camera-grid').children.length !== 3 || !$('grid-page-label').textContent.includes('5–7 of 7')) return "grid pager missing";
                if ($('video-wrap').classList.contains('inactive') ||
                    [...$('camera-grid').querySelectorAll('video')].some(video=>getComputedStyle(video).opacity !== '1')) return 'All cameras is dimmed';
                $('video-wrap').classList.add('inactive');
                const isolated = [...$('camera-grid').querySelectorAll('video')].every(video=>getComputedStyle(video).opacity === '1')
                  && getComputedStyle($('video')).opacity === '0.25';
                $('video-wrap').classList.remove('inactive');
                if (!isolated) return 'single-camera unavailable style leaks into grid';
                const gridBox = $('camera-grid').getBoundingClientRect(), pagerBox = $('grid-pager').getBoundingClientRect();
                if (pagerBox.top < gridBox.bottom || getComputedStyle($('video-note')).display !== 'none' || getComputedStyle($('video-fullscreen')).display !== 'none') return "grid controls overlap video";
                const tileRects = [...$('camera-grid').children].map(t => t.getBoundingClientRect());
                if (!tileRects.every(r => Math.abs(r.width - tileRects[0].width) < 2 && Math.abs(r.height - tileRects[0].height) < 2)) return "grid tiles uneven";
                const firstTile = $('camera-grid').children[0], savedRules = state.rules;
                state.rules = {...(savedRules || {}), rules: [{rule_id: 'rz', stream_id: firstTile.dataset.stream, enabled: true, zone: {points: [[.1,.1],[.9,.1],[.5,.9]]}}]};
                renderZoneOverlay();
                const tileZone = firstTile.querySelector('.zone-svg'), zoneBox = tileZone.getBoundingClientRect(), tileBox = firstTile.getBoundingClientRect();
                const zoneOk = tileZone.querySelectorAll('polygon').length === 1 && zoneBox.width > 20 && zoneBox.left >= tileBox.left - 1
                  && zoneBox.right <= tileBox.right + 1 && zoneBox.top >= tileBox.top - 1 && zoneBox.bottom <= tileBox.bottom + 1
                  && [...$('camera-grid').children].slice(1).every(t => !t.querySelector('.zone-svg polygon'));
                state.rules = savedRules; renderZoneOverlay();
                if (!zoneOk) return "grid tile zone missing or outside its tile";
                selectCamera("cam2");
                if (localStorage.getItem('operator.selectedCamera') !== 'cam2') return "camera not remembered";
                state.evidenceView = 'cards'; renderTracks(true); $('view-toggle').click();
                if (!$('tracks-panel').classList.contains('compact') || localStorage.getItem('operator.evidenceView') !== 'compact') return "compact evidence not remembered";
                localStorage.setItem('operator.lastActive', String(Date.now() - 360000)); state.awayChecked = false;
                state.recent.cam2 = {tracks:[{track_id:700, class_label:'person', last_observed_utc:now}]}; state.incidents[0].opened_utc = now; showAwaySummary();
                if (!$('away-summary').classList.contains('show') || !$('away-summary').textContent.includes('While you were away') || !$('away-summary').textContent.includes('Person')) return "away summary missing";
                await openDrawer(visibleTracks()[0]); await new Promise(r=>setTimeout(r,100));
                if ($('drawer-play') || $('drawer-ask')) return "drawer still has Play/Ask buttons";
                const crop = document.createElement('div'); crop.className = 'crop'; crop.style.width = '200px';
                const img = new Image(); img.src = 'data:image/svg+xml,' + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"/>');
                crop.append(img); $('drawer-body').append(crop); await img.decode();
                const box = crop.getBoundingClientRect(), shown = img.getBoundingClientRect();
                crop.remove();
                if (shown.height > box.height + 1 || shown.width > box.width + 1) return "crop image overflows its box: " + shown.height + " > " + box.height;
                const large = $('large-crop').getBoundingClientRect();
                if ($('large-crop').querySelector('img') && large.height < large.width * 0.7 && large.height < innerHeight - 240) return "drawer image not enlarged: " + large.width + "x" + large.height;
                $('search-camera').value = 'all'; closeDrawer();
                const reports = new Map([
                  ['in', {type:'inbound-rtp', kind:'video', jitterBufferDelay:.3, jitterBufferEmittedCount:3}],
                  ['cp', {type:'candidate-pair', nominated:true, currentRoundTripTime:.08}]
                ]);
                state.whep = {pc:{getStats: async () => reports}}; startLatency(state.whep); await new Promise(r=>setTimeout(r,1100));
                if (!$('latency-badge').textContent.includes('receiver est.')) return "latency badge missing";
                stopLatency(); state.whep = null;
                if ($('cameras').scrollWidth <= $('cameras').clientWidth || !$('cameras').hasAttribute('data-overflow') || getComputedStyle($('cameras')).boxShadow !== 'none') return "camera tab overflow has no neutral scroll affordance";
                renderConnectionState(true); $('connection').textContent = 'Connection lost · retrying'; $('connection').className = 'lost';
                if (!$('tracks-panel').classList.contains('stale') || !$('connection').textContent.includes('Connection lost')) return "connection lost state missing";
                renderConnectionState(false); $('connection').textContent = 'live updates'; $('connection').className = 'live';
                $('away-summary').classList.remove('show'); markActive(); state.awayChecked = true; state.evidenceView = 'cards'; selectCamera('cam1'); renderTracks(true);
                return true;
              })()`).then(done).catch(e=>done(String(e)));
            ''')
            assert new_ui is True, new_ui
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                if (state.filter.mode !== "current" || $("track-mode").value !== "current") return "wrong default view";
                await openAssistant();
                if ($("search-camera").value !== "cam1") return "scope must follow the on-screen camera";
                $("search-camera").value = "cam2";
                selectCamera("all"); if ($("search-camera").value !== "all") return "grid must scope all cameras";
                selectCamera("cam2"); if ($("search-camera").value !== "cam2") return "switch must rescope";
                selectCamera("cam1"); await openAssistant();
                if ($("search-camera").value !== "cam1") return "reopening lost the scope";
                if ([...$("search-class").options].map(o => o.value).join(",") !== ",car,chair,person") return "unsorted types";
                if ($("filter-fields").hidden || $("ask-submit").textContent !== "Send") return "wrong initial flow";
                $("ask-text").value = "How does RTSP work?"; updateQuestionFilters();
                if ($("ask-submit").textContent !== "Send") return "question action not labelled Ask";
                renderAssistant({intent:"general_chat",answer_text:"General answer.",conversation:{turns:1,characters:40},errors:[]});
                if ($("assistant-editor-error").textContent || !$("assistant-submitted").textContent.includes("General conversation")) return "general answer shown as history";
                $("assistant-reset").click();
                await new Promise(resolve => setTimeout(resolve, 50));
                if (!$("assistant-answer").textContent.includes("New conversation")) return "conversation reset failed";
                $("ask-text").value = ""; updateQuestionFilters();
                $("toggle-history-filters").click();
                $("search-camera").value = "cam1"; $("search-class").value = "person";
                const button = $("ask-submit").getBoundingClientRect().toJSON();
                await applyAssistantFilters({preventDefault(){}});
                if (state.assistantContext.payload.plan.window.minutes !== 10 || state.assistantContext.payload.plan.classes[0] !== "person") return "wrong filter plan";
                if ($("assistant-results").querySelectorAll(".track-card").length !== 1) return "imageless record shown";
                if (!$("assistant-answer").textContent.includes("4 matching tracks · 2 with images")) return "missing counts";
                await submitAssistant({page_token:state.assistantToken});
                if (!$("assistant-next").disabled || $("assistant-next").hidden || $("assistant-previous").disabled) return "paging moved";
                if ($("filter-fields").hidden || $("ask-submit").disabled) return "controls unavailable";
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
                if (state.assistantContext.payload.text !== "show cars from 4:20 to 4:25 pm yesterday" || $("ask-text").value !== "" || state.assistantContext.payload.selected_stream !== "cam1") return "question lost";
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
                    && $("assistant-editor-error").textContent.includes("Retry when ready")
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
            # Tapping a select opens the in-page touch list (48 px rows, on screen) instead of the tiny native popup.
            click('#track-mode')
            check('''(() => {
              const picker = $("select-picker"), box = picker.getBoundingClientRect(), rows = [...picker.querySelectorAll("#select-picker-list button")];
              return (picker.open && $("select-picker-title").textContent === "View" && rows.length === 3
                && rows.every(row => row.getBoundingClientRect().height >= 48) && rows[0].getAttribute("aria-selected") === "true"
                && box.top >= 0 && box.bottom <= innerHeight + 1 && box.left >= 0 && box.right <= innerWidth + 1)
                || {open: picker.open, box: box.toJSON(), rows: rows.length};
            })()''')
            click('#select-picker-list button:nth-child(2)')
            check('''(() => (!$("select-picker").open && $("track-mode").value === "history" && state.filter.mode === "history"
              && document.activeElement === $("track-mode")) || [$("select-picker").open, $("track-mode").value, state.filter.mode])()''')
            check('$("track-mode").value = "current"; $("track-mode").dispatchEvent(new Event("change")); true')
            time.sleep(1)
            check('''(() => {
              const button = document.querySelector('[data-model="chatqna"]'); button.click();
              const before = button;
              renderModels();
              const stable = !$("model-details").hidden && before === document.querySelector('[data-model="chatqna"]')
                && document.activeElement === $("close-model-details")
                && $("model-device").textContent === "GPU" && $("model-status").textContent === "Unavailable"
                && $("model-note").textContent === "Service offline. Showing configured model and device.";
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
              const near = (button) => {
                const panel = $("model-details").getBoundingClientRect(), anchor = button.getBoundingClientRect();
                return Math.abs(panel.top - anchor.bottom - 6) < 2 && panel.right <= innerWidth
                  && (Math.abs(panel.left - anchor.left) < 2 || panel.right >= innerWidth - 13);
              };
              const anchored = near(second);
              second.click(); first.click();
              const followed = near(first) && Math.round($("model-details").getBoundingClientRect().left) !== 12;
              first.click(); second.click();
              $("model-title").click();
              const panelClosed = $("model-details").hidden && document.activeElement === second && anchored && followed;
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
              state.filter.mode="history";
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
              return document.querySelector(".track-card").dataset.trackKey === "cam1:39" && !state.pending;
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
                recovered.dispatchEvent(new MessageEvent("alert", {data: JSON.stringify({alerts: [], incidents: []})}));
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
            # History beyond one page: Older/Newer pager pages each camera by offset and resets on filter change.
            result = async_js('''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const original = api, saved = state.recent.cam1, offsets = [];
                const all = Array.from({length: 120}, (_, i) => ({track_id: 1000 + i, stream_id: "cam1", class_label: "person",
                  first_observed_utc: new Date(Date.now() - i * 1000).toISOString(), last_observed_utc: new Date(Date.now() - i * 1000).toISOString(),
                  candidate_count: 0, snip_url: null, snip_state: "none"}));
                const page = (offset) => ({tracks: all.slice(offset, offset + 50), matched_count: 120, truncated: offset + 50 < 120,
                  class_counts: {person: 120}, scope: {offset}});
                api = async (path) => { const offset = Number(new URL(path, location.href).searchParams.get("offset") || 0); offsets.push(offset); return page(offset); };
                try {
                  state.paused = false; state.held = false; state.recent.cam1 = page(0); renderTracks(true);
                  const first = () => document.querySelector(".track-card")?.dataset.trackKey;
                  if ($("track-pager").hidden) return "pager hidden with 120 tracks";
                  if (!$("track-page").textContent.startsWith("Page 1 of 3") || !$("track-newer").disabled || $("track-older").disabled) return "page 1 state: " + $("track-page").textContent;
                  $("track-older").click(); await new Promise((r) => setTimeout(r, 50));
                  if (state.historyPage !== 1 || first() !== "cam1:1050" || offsets.at(-1) !== 50) return "older page not loaded: " + first();
                  if ($("track-newer").disabled || !$("list-state").textContent.includes("Older page")) return "page 2 state";
                  state.recent.cam1 = page(0); state.recent.cam1.tracks[0] = {...all[0], track_id: 9999}; renderTracks(true);
                  if (first() !== "cam1:1050") return "live update replaced older page";
                  await showHistoryPage(2);
                  if (first() !== "cam1:1100" || !$("track-older").disabled || !$("track-page").textContent.includes("101–120 of 120")) return "last page: " + $("track-page").textContent;
                  $("track-newer").click(); await new Promise((r) => setTimeout(r, 50));
                  if (state.historyPage !== 1) return "newer did not step back one page";
                  $("track-newer").click(); await new Promise((r) => setTimeout(r, 50));
                  if (state.historyPage !== 0 || first() !== "cam1:9999") return "newer did not return to live page: " + state.historyPage + " " + first();
                  await showHistoryPage(1);
                  state.recent.cam1 = page(0); await changeFilters();
                  if (state.historyPage !== 0 || Object.keys(state.pageData).length) return "filter change kept older page";
                  $("track-mode").value = "current"; state.filter.mode = "current"; renderTracks(true);
                  if (!$("track-pager").hidden) return "pager shown in current view";
                  return true;
                } finally {
                  api = original; state.recent.cam1 = saved;
                  $("track-mode").value = "history"; state.filter.mode = "history"; await changeFilters();
                }
              })()` ).then(done);
            ''')
            assert result is True, result
            # Back off failed crop URLs, then retry even when the track data is unchanged.
            check('''(() => {
              state.paused = false; state.held = false; renderTracks(true);
              const card = document.querySelector(".track-card");
              for (let attempt = 0; attempt < 2; attempt++) {
                const image = card.querySelector("img");
                image.dispatchEvent(new Event("error"));
                if (!card.dataset.imageKey || !card.querySelector(".missing") || Number(card.dataset.imageRetryAt) <= Date.now()) return "failure did not schedule back-off";
                renderTracks();
                if (card.querySelector("img")) return "failed crop retried on next live tick";
                card.dataset.imageRetryAt = String(Date.now() - 1);
                renderTracks();
                if (!card.querySelector("img") || card.querySelector("img") === image) return "unchanged crop not retried after back-off";
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
              const now = new Date().toISOString();
              state.incidents = [{incident_id: "test-incident", head_match_id: "test-alert", state: "open", stream_id: "cam2",
                class_label: "person", rule_id: "r1", rule_name: "Entry", rule_enabled: true, match_count: 1,
                opened_utc: now, last_match_utc: now, dispatch_state: "delivered", track: null, snip_url: null}];
              $('alerts').open = false;
              const video = $('video'), height = $('video-wrap').getBoundingClientRect().height;
              renderAlerts();
              const stable = !$('alerts').open && $('video') === video && $('video-wrap').getBoundingClientRect().height === height;
              const firstRow = $('alert-list').firstChild;
              state.incidents.push({...state.incidents[0],incident_id:'second-incident',head_match_id:'second-match'});
              renderAlerts();
              if ($('alert-list').firstChild !== firstRow) return 'unrelated alert rebuilt existing row';
              const secondRow = $('alert-list').lastChild;
              state.incidents[1].match_count++; renderAlerts();
              if ($('alert-list').firstChild !== firstRow || $('alert-list').lastChild === secondRow) return 'alert update replaced unaffected rows';
              $('alerts').open=true; state.incidents[1].match_count++; renderAlerts();
              if (!$('alerts').open) return 'operator-opened tray collapsed';
              state.selected = "cam3"; renderCameras(); renderTracks(true);
              const global = $("alert-summary").textContent.includes("Camera 2");
              state.incidents = []; renderAlerts(); $("alerts").open = false; $("alert-toast").hidden = true;
              state.selected = "cam1"; renderCameras(); $("track-scroll").scrollTop = 0; renderTracks(true);
              return stable && global;
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
                const surfaces = ["--bg", "--surface", "--panel", "--panel2", "--selected"].map(v => styles.getPropertyValue(v).trim());
                return [...surfaces, "#384e62", "#5b492a"].every(bg => (text+.05)/(luminance(bg)+.05) >= 4.5);
              });
            })()''')
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
            # The optional away banner must never leave an empty expanding row.
            for width, height in [(1920, 1080), (1280, 900)]:
                request('POST', base + '/window/rect', {'width': width, 'height': height})
                check('''(() => {
                  const banner = $("away-summary"), savedClass = banner.className, savedText = banner.textContent;
                  try {
                    for (const shown of [false, true, false]) {
                      banner.textContent = "While you were away";
                      banner.classList.toggle("show", shown);
                      const bounds = $("workspace").getBoundingClientRect();
                      if (Math.abs(bounds.bottom - innerHeight) > 1) return {shown, bottom:bounds.bottom, viewport:innerHeight};
                      if (shown && bounds.top < banner.getBoundingClientRect().bottom) return "workspace overlaps banner";
                    }
                    return true;
                  } finally {banner.className = savedClass; banner.textContent = savedText;}
                })()''')
            for width, height in [(1280, 800), (1280, 714), (1024, 682), (1280, 600), (1024, 540)]:
                request('POST', base + '/window/rect', {'width': width, 'height': height})
                chrome = js('return outerHeight - innerHeight')
                request('POST', base + '/window/rect', {'width': width, 'height': height + chrome})
                time.sleep(.2)
                check('state.resource = "gpu"; renderTelemetry(); true')
                # Overlay panels stay inside the viewport and keep their title/Close
                # visible when their body scrolls (1280x800 is the test display).
                check('''(() => {
                  const bad = [];
                  for (const [id, open, close] of [["cameras-panel", "cameras-toggle", "close-cameras"], ["rules-panel", "rules-toggle", "close-rules"], ["mission-panel", "mission-toggle", "close-mission"]]) {
                    $(open).click();
                    const panel = $(id); panel.scrollTop = panel.scrollHeight;
                    const box = panel.getBoundingClientRect(), head = panel.querySelector(".panel-head").getBoundingClientRect();
                    if (box.top < 0 || box.bottom > innerHeight + 1 || box.right > innerWidth + 1 || Math.abs(head.top - box.top) > 2) bad.push([id, box.toJSON(), head.toJSON()]);
                    $(close).click();
                  }
                  return bad.length ? bad : true;
                })()''')
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
                        && [...document.querySelectorAll(".quiet-button:not(#ask-submit):not(#use-viewed-camera):not(#toggle-history-filters):not(.primary-action):not(#grid-prev):not(#grid-next):not(#drawer-ask):not(#zone-toolbar *):not(#cameras-panel *)")].every(button => parseFloat(getComputedStyle(button).borderTopWidth) >= 1 && parseFloat(getComputedStyle(button).height) >= 44 && getComputedStyle(button).fontSize === "14px")
                        && ["mission-toggle","rules-toggle","audio-toggle","open-assistant"].every(id => $(id).getBoundingClientRect().height === 48 && getComputedStyle($(id)).fontSize === "15px")
                        && parseFloat(getComputedStyle(document.querySelector(".track-seen")).fontSize) >= 15
                        && document.body.scrollWidth === innerWidth && document.body.scrollHeight === innerHeight
                        && $("video-wrap").getBoundingClientRect().height >= 200
                        && $("video-wrap").getBoundingClientRect().bottom <= $("video-panel").getBoundingClientRect().bottom
                        && $("workspace").scrollWidth === $("workspace").clientWidth
                        && chart.height >= ({str(expanded).lower()} ? 85 : 50) && chart.bottom <= panel.bottom
                        && (!{str(expanded).lower()} || $("chart-note").getBoundingClientRect().bottom <= panel.bottom)
                        && ({str(expanded).lower()} || innerHeight <= 650 || $("video-panel").getBoundingClientRect().height >= 1.5 * panel.height)
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
                  // The welcome state is what a demonstration opens on, and it is
                  // taller than a results view. The question box must be reachable
                  // without hunting for an inner scroll container.
                  openAssistant();
                  $("assistant-panel").classList.add("welcome");
                  const fits = (id) => {
                    const box = $(id).getBoundingClientRect();
                    return box.top >= 0 && box.bottom <= innerHeight + 1
                      && box.left >= 0 && box.right <= innerWidth + 1;
                  };
                  const unreachable = ["ask-text", "ask-submit", "search-camera"].filter(id => !fits(id));
                  closeAssistant();
                  return unreachable.length === 0
                    || {welcomeComposerOffscreen: unreachable, viewport: [innerWidth, innerHeight]};
                })()''')
                check('''(() => {
                  openAssistant();
                  renderAssistant({answer_text:"Choose an object type and time window."});
                  const intro = $("assistant-panel").getBoundingClientRect();
                  if ($("assistant-paging").getBoundingClientRect().bottom > intro.bottom + 1) return {introOverflow: [innerWidth, innerHeight]};
                  renderAssistant({answer_text:"No matching records.", query:{tracks:[], total:0, with_crops_total:0,
                    page:0, page_size:20, filters:{streams:["cam1"], classes:null},
                    window:{started_utc:new Date().toISOString(), ended_utc:new Date().toISOString()}, warnings:[]}});
                  if (!$("assistant-panel").classList.contains("filters-open")) $("toggle-history-filters").click();
                  const panel = $("assistant-panel").getBoundingClientRect();
                  const paging = $("assistant-paging").getBoundingClientRect();
                  const host = $("tracks-panel").getBoundingClientRect();
                  // On a short screen the open assistant is anchored to the
                  // viewport instead of the evidence column, so it is bounded by
                  // the window rather than by its host panel.
                  const overlay = getComputedStyle($("assistant-panel")).position === "fixed";
                  const bounded = overlay
                    ? panel.left >= 0 && panel.right <= innerWidth + 1
                      && panel.top >= 0 && panel.bottom <= innerHeight + 1
                    : panel.left >= host.left && panel.right <= host.right + 1;
                  const ok = bounded
                    && $("video-panel").inert === overlay && !$("telemetry").inert
                    && !$("search-mode") && $("ask-text").getBoundingClientRect().height >= 48
                    && paging.bottom <= panel.bottom + 1
                    && $("assistant-results").clientHeight >= 80
                    && ["search-camera","search-class","search-period"].every(id => $(id).getBoundingClientRect().height >= 48)
                    && ["close-assistant","assistant-previous","assistant-next"].every(id => $(id).getBoundingClientRect().height >= 44) && $("ask-submit").getBoundingClientRect().height >= 48;
                  const header=document.querySelector("header").getBoundingClientRect();
                  const contextVisible=!overlay || (panel.top>=header.bottom && $("close-assistant").textContent==="Back to camera");
                  $("mission-toggle").click();
                  const missionVisible=!$("assistant-panel").classList.contains("open") && !$("mission-panel").hidden;
                  closeMission(false);closeAssistant();
                  return (ok && contextVisible && missionVisible) || {panel:panel.toJSON(), paging:paging.toJSON(), overlay,contextVisible,missionVisible};
                })()''')
                if args.screenshots:
                    check('renderChart(); true')
                    data = request('GET', base + '/screenshot')
                    (args.screenshots / f'fixture-{width}x{height}.png').write_bytes(base64.b64decode(data))
            # Small upward wheel steps (smooth scrolling) must not snap the conversation back to the latest reply.
            result = async_js(r'''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                await openAssistant(); await $("assistant-reset").onclick();
                const frame = () => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                const view = $("assistant-output"), turns = [];
                $("assistant-panel").classList.remove("welcome");
                for (let i = 0; i < 5; i++) {
                  const turn = document.createElement("div"); turn.className = "chat-turn";
                  turn.innerHTML = "<p class=chat-question>Question</p><p>" + "Answer text. ".repeat(60) + "</p>";
                  $("assistant-transcript").append(turn); turns.push(turn);
                }
                scrollChat(true); await frame();
                const bottom = view.scrollTop;
                view.scrollTop = bottom - 20; await frame();
                view.scrollTop = bottom - 40; await frame();
                const held = view.scrollTop === bottom - 40;
                view.scrollTop = bottom - 300; await frame();
                $("assistant-answer").textContent += " more"; scrollChat(); await frame();
                const stays = view.scrollTop === bottom - 300 && !$("jump-latest").hidden;
                $("jump-latest").click(); await frame();
                const follows = view.scrollHeight - view.clientHeight - view.scrollTop < 2 && $("jump-latest").hidden;
                turns.forEach(turn => turn.remove()); $("assistant-answer").textContent = "";
                await $("assistant-reset").onclick(); closeAssistant(false);
                return (bottom > 300 && held && stays && follows) || {bottom, held, stays, follows};
              })()`).then(done, error => done(String(error)));''')
            assert result is True, result
            result = async_js(r'''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                await openAssistant(); await $("assistant-reset").onclick();
                const original = window.fetch;
                let controller;
                const encoder = new TextEncoder();
                try {
                  window.fetch = async (url, options) => {
                    if (url !== "/api/ask") return original(url, options);
                    return new Response(new ReadableStream({start(c){controller=c;}}),
                      {headers:{"content-type":"application/x-ndjson"}});
                  };
                  const firstId = conversationId;
                  $("ask-text").value = "Explain daylight";
                  const pending = submitAssistant({text:"Explain daylight",mode:"general"});
                  await new Promise(r=>setTimeout(r,20));
                  controller.enqueue(encoder.encode(JSON.stringify({delta:"First words"})+"\\n"));
                  await new Promise(r=>setTimeout(r,20));
                  if ($("assistant-answer").textContent !== "First words") return "tokens not visible before completion";
                  await $("assistant-reset").onclick();
                  if (conversationId === firstId || $("ask-text").value) return "new conversation kept old identity/input";
                  controller.enqueue(encoder.encode(JSON.stringify({answer:{intent:"general_chat",answer_text:"Late old answer",errors:[]},done:true})+"\\n"));
                  controller.close(); await pending;
                  if (!$("assistant-answer").textContent.startsWith("New conversation")) return "late result entered new conversation";
                  state.assistantContext = {text:"First question",payload:{}};
                  renderAssistant({intent:"general_chat",answer_text:"First answer",errors:[]});
                  archiveAssistantTurn();
                  renderAssistant({intent:"general_chat",answer_text:"Second answer",errors:[]});
                  if (!$("assistant-transcript").textContent.includes("First answer")) return "previous exchange lost";
                  await $("assistant-reset").onclick();
                  if ($("assistant-transcript").children.length) return "new conversation kept transcript";
                  return true;
                } finally {window.fetch=original;}
              })()`).then(done).catch(e=>done(String(e)));
            ''')
            assert result is True, result
            result = async_js(r'''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                await openAssistant(); await $("assistant-reset").onclick();
                if (!$("assistant-panel").classList.contains("welcome") || $("start-people").getBoundingClientRect().height < 44) return "missing usable starting points";
                if ($("start-explain").getBoundingClientRect().bottom > $("assistant-output").getBoundingClientRect().bottom + 1) return "starting points clipped on small screen";
                $("start-explain").click();
                if ($("assistant-mode").value !== "general" || document.activeElement.id !== "ask-text") return "explain action did not prepare composer";
                state.selected = "cam1"; $("search-camera").value = "all"; updateAssistantCameraCue();
                if ($("use-viewed-camera").hidden) return "no explicit camera shortcut";
                $("use-viewed-camera").click();
                if ($("search-camera").value !== "cam1") return "wrong camera selected";
                await $("start-people").onclick();
                if (state.assistantContext.payload.plan.classes[0] !== "person" || state.assistantContext.payload.selected_stream !== "cam1" || state.assistantContext.payload.plan.window.minutes !== 10) return "starter search scope wrong";
                const classified = structuredClone(state.assistantAnswer);
                classified.query.filters.attributes = [{name:"surface_finish", value:"A42"}];
                renderAssistant(classified);
                if (!$("assistant-scope").textContent.includes("surface_finish: A42")) return "attribute filter not visible";
                if (!$("answer-actions").textContent.includes("Compare Camera 2")) return "no camera comparison action";
                const compare = [...$("answer-actions").children].find(b=>b.textContent==="Compare Camera 2");
                await compare.onclick();
                if (state.assistantContext.payload.selected_stream !== "cam2" || state.assistantContext.payload.plan.window.kind !== "absolute") return "comparison lost exact window";
                if (state.assistantContext.payload.plan.attributes[0].value !== "A42") return "comparison dropped attribute filter";
                renderAssistant({intent:"clarification",answer_text:"Requested field unavailable.",errors:[],
                  broader_search:{excluded_attributes:[{name:"unrecorded_field",value:"x"}],excluded_conditions:["an unresolved condition"],
                    selected_stream:"cam1", plan:{window:{kind:"absolute",start_utc:"2026-09-21T10:00:00Z",end_utc:"2026-09-21T10:10:00Z"},
                      classes:["person"],streams:["cam1"],attributes:[{name:"surface_finish",value:"A42"}]}}});
                if (!$("answer-actions").textContent.includes("will not check: unrecorded_field=x, an unresolved condition")) return "broader search hides dropped condition";
                const broader = [...$("answer-actions").children].find(b=>b.textContent==="Show broader results");
                $("assistant-panel").classList.remove("filters-open");
                $("toggle-history-filters").setAttribute("aria-expanded", "false");
                const composerBefore = $("ask-form").getBoundingClientRect();
                const inputBefore = $("ask-text").getBoundingClientRect();
                await broader.onclick();
                const composerAfter = $("ask-form").getBoundingClientRect();
                if ($("assistant-panel").classList.contains("filters-open")
                    || $("toggle-history-filters").getAttribute("aria-expanded") !== "false"
                    || Math.abs(composerBefore.bottom - composerAfter.bottom) > 1
                    || Math.abs(inputBefore.top - $("ask-text").getBoundingClientRect().top) > 1
                    || composerAfter.top < $("assistant-output").getBoundingClientRect().bottom - 1)
                  return "broader results moved composer or expanded filters";
                $("toggle-history-filters").click();
                renderAssistant(state.assistantAnswer);
                if (!$("assistant-panel").classList.contains("filters-open")) return "answer collapsed operator-opened filters";
                $("toggle-history-filters").click();
                if (state.assistantContext.payload.plan.attributes[0].value !== "A42"
                    || state.assistantContext.payload.selected_stream !== "cam1"
                    || !state.assistantContext.text.includes("not checked: unrecorded_field=x")) return "broader search changed scope or lost disclosure";
                renderAssistant({intent:"general_chat",answer_text:"An explanation.",errors:[]});
                if (!$("answer-actions").textContent.includes("Explain simply") || !$("answer-actions").textContent.includes("Copy answer")) return "missing answer actions";
                const shiftEnter = new KeyboardEvent("keydown",{key:"Enter",shiftKey:true,cancelable:true});
                $("ask-text").dispatchEvent(shiftEnter);
                if (shiftEnter.defaultPrevented || $("ask-text").tagName !== "TEXTAREA") return "multiline composer unavailable";
                for(let i=0;i<20;i++) {
                  const item=document.createElement("p");item.textContent="Earlier message "+i;item.style.height="40px";$("assistant-transcript").append(item);
                }
                $("assistant-output").scrollTop=0;
                $("assistant-output").dispatchEvent(new Event("scroll"));
                $("assistant-answer").textContent += "More streamed text.";
                scrollChat();
                if ($("assistant-output").scrollTop !== 0 || $("jump-latest").hidden) return "reading position stolen";
                $("jump-latest").click();
                if ($("assistant-output").scrollTop === 0 || !$("jump-latest").hidden) return "jump to latest failed";
                await $("assistant-reset").onclick();
                return true;
              })()`).then(done).catch(e=>done(String(e)));
            ''')
            assert result is True, result
            result = async_js(r'''const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                await detachWhep();
                const realPC = window.RTCPeerConnection, realFetch = window.fetch;
                const realTimeout = window.setTimeout, realClear = window.clearTimeout;
                const timers = new Map(), peers = [], deleted = [];
                let id = -1, mode = "ok", held;
                const assert = (ok, message) => { if (!ok) throw Error(message); };
                const settle = () => new Promise(resolve => realTimeout(resolve, 20));
                const fire = async (key) => {
                  const timer = timers.get(key); assert(timer, "missing timer " + key);
                  timers.delete(key); timer.fn(); await settle();
                };
                class Peer {
                  constructor() { this.connectionState = this.iceConnectionState = "new";
                    this.iceGatheringState = mode === "ice" ? "gathering" : "complete"; peers.push(this); }
                  addTransceiver() {}
                  async createOffer() { return {type:"offer", sdp:"fixture"}; }
                  async setLocalDescription(value) { this.localDescription = value; }
                  async setRemoteDescription() {
                    this.connectionState = this.iceConnectionState = "connected";
                    this.track = {muted:false};
                    this.ontrack({streams:[new MediaStream()],track:this.track});
                    this.onconnectionstatechange();
                  }
                  close() { this.connectionState = "closed"; this.onconnectionstatechange?.(); }
                }
                try {
                  window.RTCPeerConnection = Peer;
                  window.setTimeout = (fn, ms, ...args) => {
                    if (![1000,2000,3000,4000,8000,10000,15000,16000,30000].includes(ms)) return realTimeout(fn,ms,...args);
                    const key = id--; timers.set(key,{fn,ms}); return key;
                  };
                  window.clearTimeout = key => { timers.delete(key); realClear(key); };
                  window.fetch = async (url, options = {}) => {
                    if (options.method === "DELETE" && String(url).includes("/session/")) {
                      deleted.push(String(url)); return new Response(null,{status:204});
                    }
                    if (options.method !== "POST" || !String(url).endsWith("/whep")) return realFetch(url,options);
                    if (mode === "hold") return new Promise(resolve => { held = resolve; });
                    if (mode === "missing") return new Response("no one is publishing",{status:400});
                    if (mode === "codec") return new Response("no supported codec",{status:400});
                    if (mode === "fail") return new Response("unavailable",{status:503});
                    return new Response("answer",{status:201,headers:{Location:"/session/"+peers.length}});
                  };
                  const [one,two] = state.streams;
                  state.selected = one.stream_id; await attachWhep(one);
                  const first = state.whep.pc;
                  first.connectionState = "failed"; first.onconnectionstatechange();
                  assert($("video-note").textContent.includes("Reconnecting"),"no visible retry");
                  await fire(state.whep.retryTimer);
                  assert(state.whep.pc !== first && first.connectionState === "closed" && deleted.length === 1,"failed peer not replaced/cleaned");
                  const healthy = state.whep.pc;
                  healthy.connectionState = "disconnected"; healthy.onconnectionstatechange();
                  healthy.connectionState = "connected"; healthy.onconnectionstatechange();
                  assert(!state.whep.disconnectTimer && state.whep.pc === healthy,"brief interruption replaced peer");
                  healthy.track.onmute(); await fire(state.whep.mediaTimer); await fire(state.whep.retryTimer);
                  assert(state.whep.pc !== healthy,"muted video did not reconnect");
                  mode = "fail"; await attachWhep(one);
                  for (const delay of [1000,2000,4000,8000,16000,30000,30000]) {
                    assert(timers.get(state.whep.retryTimer)?.ms === delay,"incorrect retry backoff");
                    await fire(state.whep.retryTimer);
                  }
                  mode = "ok"; window.dispatchEvent(new Event("online")); await settle();
                  assert(state.whep.pc.connectionState === "connected","network return did not recover");
                  mode = "missing"; await attachWhep(one);
                  assert(state.whep.retryTimer,"missing publisher must retry");
                  mode = "codec"; await attachWhep(one);
                  assert(!state.whep.retryTimer && $("video-note").textContent.includes("compatible browser"),"codec failure loops");
                  mode = "hold"; const pending = attachWhep(one); await settle();
                  state.selected = two.stream_id; mode = "ok"; await attachWhep(two);
                  const selectedPeer = state.whep.pc;
                  held(new Response("late answer",{status:201,headers:{Location:"/session/stale"}})); await pending;
                  assert(state.whep.pc === selectedPeer && deleted.some(url=>url.endsWith("/session/stale")),"late session leaked or stole camera");
                  state.selected = one.stream_id; mode = "ice";
                  const gathering = attachWhep(one); await settle(); await fire(state.whep.timeout); await gathering;
                  assert(state.whep.retryTimer,"ICE timeout did not retry");
                  state.gridPage = 0; selectCamera("all"); await settle();
                  assert(!state.whep && state.gridWheps.size <= 4,"All cameras did not switch to grid sessions");
                  const gridVideos = [...$('camera-grid').querySelectorAll('video')];
                  const gridPeers = [...state.gridWheps.values()].map(c => c.pc);
                  renderCameraGrid();
                  assert(gridVideos.every((v,i) => v === $('camera-grid').querySelectorAll('video')[i]), "grid redraw replaced video elements");
                  assert(gridPeers.every((pc,i) => pc === [...state.gridWheps.values()][i].pc), "grid redraw replaced peers");
                  const gridCurrent = state.gridWheps.get(one.stream_id);
                  retryGridWhep(gridCurrent); await settle();
                  const retryTimer = gridCurrent.retryTimer;
                  retryGridWhep(gridCurrent); await settle();
                  assert(retryTimer === gridCurrent.retryTimer, "grid close scheduled duplicate retries");
                  mode = "ok"; await fire(retryTimer);
                  const restored = state.gridWheps.get(one.stream_id);
                  assert(restored.pc.connectionState === "connected", "grid retry failed");
                  restored.pc.track.onmute(); await fire(restored.mediaTimer); await fire(restored.retryTimer);
                  assert(state.gridWheps.get(one.stream_id).pc !== restored.pc, "stalled grid track did not reconnect");
                  await detachGridWheps();
                  mode = "hold";
                  const late = attachGridWhep(one, gridVideos[0], document.createElement('small'));
                  await settle(); await detachGridWheps();
                  held(new Response("late answer",{status:201,headers:{Location:"/session/grid-stale"}})); await late;
                  assert(deleted.some(url => url.endsWith('/session/grid-stale')), "grid late session leaked");
                  mode = "ok"; resumeWhep(); await settle();
                  assert(state.gridWheps.size > 0 && [...state.gridWheps.values()].every(c => c.video.isConnected), "All cameras did not resume after page suspension");
                  await detachGridWheps(); timers.clear();
                  return true;
                } finally {
                  state.selected = "all"; await detachWhep();
                  window.RTCPeerConnection = realPC; window.fetch = realFetch;
                  window.setTimeout = realTimeout; window.clearTimeout = realClear;
                }
              })()`).then(done).catch(e=>done(String(e)));
            ''')
            assert result is True, result
            js("window.eval('closeAssistant(false)')")
            assert js("""const button=document.getElementById('voice-talk'), box=button.getBoundingClientRect();
              return button.closest('header') && !button.closest('[inert]') && box.height>=44
                && box.top>=0 && box.right<=innerWidth && getComputedStyle(button).display!=='none';"""), 'Talk must be visible without opening Assistant'
            assert js("""return window.eval(`(()=>{
              const saved=state.speech, audio=state.audioOn;
              try {
                state.audioOn=true;
                state.speech={preparing:'Answer text.',preparing_kind:'answer'};renderSpeech();
                if($('speech-line').textContent || !$('assistant-speech-line').textContent.includes('Preparing speech')) return false;
                state.speech={last:{kind:'answer',outcome:'text_only',text:'Answer text.'}};renderSpeech();
                if($('speech-line').textContent || !$('assistant-speech-line').textContent.includes('Text only')) return false;
                state.speech={last:{kind:'alert',outcome:'text_only',text:'Person detected.'}};renderSpeech();
                return !$('assistant-speech-line').textContent && $('speech-line').textContent.includes('Person detected.');
              } finally {state.speech=saved;state.audioOn=audio;renderSpeech();}
            })()` )"""), 'Assistant speech leaked into alerts'
            voice_check = async_js('''const done=arguments[arguments.length-1];
              window.eval(`(async()=>{
                const original={postApi,submitAssistant,fetch:window.fetch,AudioContext:window.AudioContext,
                  getUserMedia:navigator.mediaDevices.getUserMedia};
                let stops=0,closed=0,submitted=null,held=[],uploaded=false,cancels=0,stall=false;
                const assert=(ok,message)=>{if(!ok)throw Error(message);};
                const node=()=>({connect(){},disconnect(){}});
                class Context {
                  sampleRate=48000; destination={};
                  async resume(){} async close(){closed++;}
                  createMediaStreamSource(){return node();}
                  createScriptProcessor(){return node();}
                  createGain(){return {...node(),gain:{value:1}};}
                }
                const stream=()=>({getTracks(){return this.getAudioTracks();},getAudioTracks(){return [this.track];},
                  track:{readyState:'live',stop(){stops++;}}});
                try {
                  window.AudioContext=Context;
                  navigator.mediaDevices.getUserMedia=async()=>stream();
                  postApi=async(path,body)=>{if(path.endsWith('/talk')) held.push(body.held+(body.answer_pending?'+pending':''));return {};};
                  submitAssistant=async payload=>{submitted=payload;};
                  window.fetch=async(path,options)=>{
                    if(path==='/api/voice/cancel') {cancels++;return new Response('{}');}
                    if(!String(path).startsWith('/api/voice/transcribe')) return original.fetch(path,options);
                    if(stall) return new Promise((_,reject)=>options.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))));
                    const bytes=await options.body.arrayBuffer(), view=new DataView(bytes);
                    assert(view.getUint32(24,true)===16000 && view.getUint16(22,true)===1,'not mono 16k WAV');
                    uploaded=true;return new Response(JSON.stringify({data:{text:'How many people?'}}),{status:200});
                  };
                  state.assistantBusy=false; $('search-camera').value='cam2'; $('assistant-mode').value='auto';
                  $('voice-talk').click();
                  while(!voice.turn?.processor) await new Promise(resolve=>setTimeout(resolve,0));
                  assert($('assistant-panel').classList.contains('open'),'Talk did not open Assistant');
                  assert($('voice-talk').getAttribute('aria-pressed')==='true','capture indicator missing');
                  voice.turn.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4800).fill(.2)}});
                  await endVoice();
                  assert(uploaded && submitted.text==='How many people?' && submitted.selected_stream==='cam2' && submitted.mode==='auto','voice bypasses typed request context');
                  assert(stops===1 && closed===1 && held.join(',')==='true,true,false+pending','capture resources or talk hold leaked');
                  let permission;
                  navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>permission=resolve);
                  const pending=beginVoice();
                  while(!permission) await new Promise(resolve=>setTimeout(resolve,0));
                  await endVoice(false); permission(stream()); await pending;
                  assert(!voice.turn && stops===2 && closed===2,'late permission started capture');
                  navigator.mediaDevices.getUserMedia=async()=>{throw new DOMException('denied','NotAllowedError');};
                  await beginVoice();
                  assert(!voice.turn && $('voice-status').textContent.includes('permission denied'),'denial has no recovery');
                  assert(!$('ask-text').disabled && !$('ask-submit').disabled,'typing blocked after failure');
                  navigator.mediaDevices.getUserMedia=async()=>stream(); stall=true; $('voice-talk').click();
                  while(!voice.turn?.processor) await new Promise(resolve=>setTimeout(resolve,0));
                  voice.turn.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(4800).fill(.2)}});
                  const sending=endVoice();
                  while($('voice-status').textContent!=='Transcribing…') await new Promise(resolve=>setTimeout(resolve,0));
                  cancelVoice(); await sending; stall=false;
                  assert(cancels===1 && !voice.turn && $('voice-status').textContent==='Voice request cancelled.','cancel during transcription left server cue');
                  held.length=0; $('voice-talk').click();
                  while(!voice.turn?.processor) await new Promise(resolve=>setTimeout(resolve,0));
                  await endVoice();
                  assert(held.at(-1)==='false' && cancels===1,'silent release held answer speech');
                  return true;
                } finally {
                  cancelVoice();postApi=original.postApi;submitAssistant=original.submitAssistant;
                  window.fetch=original.fetch;window.AudioContext=original.AudioContext;
                  navigator.mediaDevices.getUserMedia=original.getUserMedia;
                }
              })()`).then(done).catch(e=>done(String(e)));
            ''')
            assert voice_check is True, voice_check
            recovery = async_js(r"""const done = arguments[arguments.length - 1];
              window.eval(`(async () => {
                const original={fetch:window.fetch,postApi,compactAssistant,getUserMedia:navigator.mediaDevices.getUserMedia};
                const assert=(ok,message)=>{if(!ok) throw Error(message);};
                try {
                  compactAssistant=()=>true; sizeAssistant();
                  assert(!$('alert-toast').closest('[inert]'),'alert toast inaccessible in compact Assistant');
                  compactAssistant=original.compactAssistant; sizeAssistant();
                  state.assistantBusy=true;$('ask-text').disabled=true;
                  const version=assistantRequestVersion;
                  navigator.mediaDevices.getUserMedia=undefined;
                  await beginVoice();
                  assert(state.assistantBusy && assistantRequestVersion===version,'failed microphone discarded active question');
                  navigator.mediaDevices.getUserMedia=original.getUserMedia;
                  state.assistantBusy=false;$('ask-text').disabled=false;
                  for(const outcome of ['playback_failed','unconfirmed']) {
                    state.speech={current:null,last:{kind:'answer',outcome,text:'Retained answer'}}; renderSpeech();
                    assert($('assistant-speech-line').textContent.includes('Retained answer'),'playback failure vanished');
                  }
                  state.audioOn=true;state.playback=null;voiceStatus('Microphone permission denied.');
                  state.speech={current:{id:'a1',kind:'alert',text:'dog on Camera 2.'}};renderSpeech();
                  assert($('voice-status').textContent==='Microphone permission denied.','alert speech overwrote voice status');
                  state.assistantBusy=true;state.speech={current:{id:'c1',kind:'answer',text:'Checking.'}};renderSpeech();
                  assert($('voice-status').textContent==='Preparing speech…','answer speech status missing');
                  state.speech={current:null,last:{kind:'answer',outcome:'spoken',text:'Checking.'}};renderSpeech();
                  assert($('voice-status').textContent==='Answering…','status stuck after cue while answering');
                  state.assistantBusy=false;state.speech={current:{id:'c2',kind:'answer',text:'Done.'}};renderSpeech();
                  state.speech={current:null,last:{kind:'answer',outcome:'spoken',text:'Done.'}};renderSpeech();
                  assert($('voice-status').textContent.startsWith('Done.'),'status stuck after answer speech');
                  stopPlayback();
                  state.speech=null;state.audioOn=false;
                  window.fetch=async()=>new Response([{delta:'Useful partial answer.'},{error:'Disconnected'}].map(e=>JSON.stringify(e)+String.fromCharCode(10)).join(''),
                    {headers:{'content-type':'application/x-ndjson'}});
                  await submitAssistant({text:'Which objects?',selected_stream:'cam2',mode:'auto'});
                  assert($('assistant-answer').textContent==='Useful partial answer.','partial answer erased');
                  assert($('assistant-scope').textContent.includes('Incomplete') && $('answer-actions').textContent.includes('Retry'),'partial answer has no recovery');
                  $('ask-text').value='';$('edit-question').click();
                  assert($('ask-text').value==='Which objects?' && $('search-camera').value==='cam2','edit lost original question/scope');
                  let aborted=false, requested=false;
                  postApi=async()=>({});
                  window.fetch=async(url,options)=>{
                    requested=true;
                    return new Response(new ReadableStream({start(controller){
                      options.signal.addEventListener('abort',()=>{aborted=true;controller.error(new DOMException('Cancelled','AbortError'));});
                    }}),{headers:{'content-type':'application/x-ndjson'}});
                  };
                  const pending=submitAssistant({text:'Long request',mode:'general'});
                  while(!requested) await new Promise(resolve=>setTimeout(resolve,0));
                  await cancelQuestion();await pending;
                  assert(aborted && !state.assistantBusy && !$('ask-text').disabled && $('voice-cancel').hidden,'cancel did not restore composer');
                  assert($('assistant-warnings').textContent.includes('cancelled'),'cancel has no visible outcome');
                  return true;
                } finally {
                  window.fetch=original.fetch;postApi=original.postApi;compactAssistant=original.compactAssistant;
                  navigator.mediaDevices.getUserMedia=original.getUserMedia;
                  state.assistantBusy=false;sizeAssistant();
                }
              })()`).then(done).catch(error=>done(String(error)));
            """)
            assert recovery is True, recovery
            # Cameras panel: add a publisher camera (no camera limit), remove behind a confirm.
            js("closeAssistant(false); closeDrawer();")
            click('#cameras-toggle')
            for _ in range(100):
                if js("return document.getElementById('camera-count').textContent === '2 cameras on'"):
                    break
                time.sleep(.05)
            cams = js('return window.eval(' + json.dumps('''(() => {
              const items = [...$("camera-list").children];
              const open = !$("cameras-panel").hidden && $("cameras-toggle").getAttribute("aria-expanded") === "true";
              const rows = items.length === 2 && items[1].textContent.includes("Network camera · rtsp://192.168.1.9/s");
              items[0].querySelector(".camera-edit-toggle").click();
              const config = $("camera-list").children[0].textContent.includes("stays off until you turn it on")
                && !!$("camera-list").children[0].querySelector(".camera-actions")
                && !$("camera-list").children[0].querySelector(".camera-remove");
              const addHidden = $("camera-add").hidden && !$("camera-add-open").hidden;
              $("camera-add-open").click();
              const addShown = !$("camera-add").hidden && $("camera-add-open").hidden
                && $("cameras-toggle-count").textContent === state.streams.length + " on · manage";
              $("camera-add-submit").click();
              const empty = $("camera-status").textContent === "Give the camera a name.";
              const pullHidden = $("camera-url-label").hidden;
              document.querySelector('input[name="camera-source"][value="pull"]').click();
              const pullShown = !$("camera-url-label").hidden && $("camera-add-note").textContent.includes("192.168.0.0/16");
              document.querySelector('input[name="camera-source"][value="publish"]').click();
              $("camera-name").value = "Dock";
              $("camera-add-submit").click();
              return {open, rows, config, addHidden, addShown, empty, pullHidden, pullShown};
            })()''') + ')')
            assert all(cams.values()), cams
            for _ in range(100):
                if js("return !document.getElementById('camera-added').hidden && document.getElementById('camera-added').textContent.includes('Waiting for video') && document.querySelector('#camera-added code')?.textContent.startsWith('rtsp://127.0.0.1:8554/cam') && document.activeElement?.textContent === 'Copy'"):
                    break
                time.sleep(.05)
            else:
                raise AssertionError('post-add publish address card missing')
            added = CAMERAS[-1]['stream_id']
            assert CAMERA_REQUESTS[-1] == ('POST', '/api/cameras', {'display_name': 'Dock', 'source': {'kind': 'publish'}}), CAMERA_REQUESTS
            limit = js('return window.eval(' + json.dumps('''(() => {
              const more = !$("camera-add-open").disabled && $("camera-count").textContent === "3 cameras on";
              $("camera-list").querySelector('li[data-stream="ADDED"] .camera-edit-toggle').click();
              const item = $("camera-list").querySelector('li[data-stream="ADDED"]');
              const editing = !!item.querySelector(".camera-edit") && item.textContent.includes("rtsp://127.0.0.1:8554/ADDED");
              item.querySelector(".camera-remove button").click();
              const again = $("camera-list").querySelector('li[data-stream="ADDED"]');
              const asks = again.textContent.includes("Recorded history and alerts stay") && again.querySelectorAll(".camera-remove button").length === 2;
              again.querySelector(".camera-remove .danger").click();
              return {more, editing, asks};
            })()'''.replace('ADDED', added)) + ')')
            assert all(limit.values()), limit
            for _ in range(100):
                if js("return document.getElementById('camera-count').textContent === '2 cameras on'"):
                    break
                time.sleep(.05)
            assert CAMERA_REQUESTS[-1][:2] == ('DELETE', f'/api/cameras/{added}?confirm={added}'), CAMERA_REQUESTS
            assert js("return document.getElementById('camera-status').textContent.includes('removed. Its history is kept.')")
            assert js('return ruleSummary({stream_id:"cam99",class_label:null,dwell_seconds:null}).includes("(camera off or removed)")')
            zone_order = js('return window.eval(' + json.dumps('''(() => {
              const square = zoneInsert([[.1,.1],[.9,.1],[.1,.9]], [.9,.9]);
              const ordered = JSON.stringify(square) === JSON.stringify([[.1,.1],[.9,.1],[.9,.9],[.1,.9]]);
              const bowtie = zoneCrosses([[.1,.1],[.9,.1],[.1,.9],[.9,.9]]) && !zoneCrosses(square);
              const appended = JSON.stringify(zoneInsert([[.1,.1],[.9,.1],[.9,.9]], [.1,.9])) === JSON.stringify([[.1,.1],[.9,.1],[.9,.9],[.1,.9]]);
              const quad = [[.1,.1],[.9,.1],[.9,.9],[.1,.9]];
              const fifth = JSON.stringify(zoneInsert(quad, [.05,.5])) === JSON.stringify([...quad, [.05,.5]]);
              const reordered = JSON.stringify(zoneInsert(quad, [.5,.05])) === JSON.stringify([[.1,.1],[.5,.05],[.9,.1],[.9,.9],[.1,.9]]);
              state.zoneDraft = {points: [[.1,.1],[.9,.1],[.1,.9]]}; state.zoneAdded = [];
              $("zone-editor").hidden = false; drawZoneDraft();
              const r = $("zone-svg").getBoundingClientRect();
              $("zone-svg").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, clientX: r.left + r.width * .9, clientY: r.top + r.height * .9}));
              const tapped = state.zoneDraft.points.length === 4 && Math.abs(state.zoneDraft.points[2][0] - .9) < .01 && !$("zone-done").disabled;
              state.zoneDraft.points = [[.1,.1],[.9,.1],[.9,.9],[.1,.9]]; state.zoneAdded = [];
              $("zone-svg").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, clientX: r.left + r.width * .05, clientY: r.top + r.height * .5}));
              const kept = state.zoneDraft.points.length === 5 && Math.abs(state.zoneDraft.points[4][0] - .05) < .01 && !$("zone-step").textContent.includes("renumbered");
              $("zone-undo").click();
              $("zone-svg").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, clientX: r.left + r.width * .5, clientY: r.top + r.height * .05}));
              const noted = Math.abs(state.zoneDraft.points[1][0] - .5) < .01 && $("zone-step").textContent.includes("Corner 2 placed between 1 and 3");
              $("zone-undo").click();
              state.zoneDraft.points = [[.1,.1],[.9,.1],[.1,.9]]; state.zoneAdded = [];
              $("zone-svg").dispatchEvent(new PointerEvent("pointerdown", {bubbles: true, clientX: r.left + r.width * .9, clientY: r.top + r.height * .9}));
              $("zone-undo").click();
              const undone = JSON.stringify(state.zoneDraft.points) === JSON.stringify([[.1,.1],[.9,.1],[.1,.9]]);
              state.zoneDraft.points = [[.1,.1],[.9,.1],[.1,.9],[.9,.9]]; drawZoneDraft();
              const blocked = $("zone-done").disabled && $("zone-step").textContent.includes("crosses");
              $("zone-editor").hidden = true; state.zoneDraft = null;
              return {ordered, bowtie, appended, fifth, reordered, tapped, kept, noted, undone, blocked};
            })()''') + ')')
            assert all(zone_order.values()), zone_order
            js("document.activeElement.blur(); document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}))")
            assert js("return document.getElementById('cameras-panel').hidden"), 'Escape left Cameras open'
            if args.screenshots:
                args.screenshots.mkdir(parents=True, exist_ok=True)
                request('POST', base + '/window/rect', {'width': 1280, 'height': 800})
                js('return window.eval(' + json.dumps('showAlertError(""); renderConnectionState(false); $("connection").textContent="live updates"; $("connection").className="live";') + ')')
                def shot(name):
                    (args.screenshots / name).write_bytes(base64.b64decode(request('GET', base + '/screenshot')))
                js('return window.eval(' + json.dumps('(() => { const now=new Date().toISOString(); closeAssistant(false); closeDrawer(); state.announced=new Set(["shot"]); state.incidents=[{incident_id:"shot",head_match_id:"shot",state:"open",stream_id:"cam2",class_label:"person",rule_id:"r1",rule_name:"perimeter dwell rule",rule_enabled:true,match_count:3,opened_utc:now,last_match_utc:now,dispatch_state:"delivered",speech:"unconfirmed",snip_url:"/fixture.svg",track:null}]; selectCamera("cam1"); renderAlerts(); $("alerts").open=true; })()') + ')')
                shot('alert-row.png')
                js('return window.eval(' + json.dumps('state.incidents[0].stream_id="cam6"; renderAlerts(); selectCamera("all"); state.gridPage=1; renderCameraGrid();') + ')')
                shot('camera-grid-pager.png')
                js('return window.eval(' + json.dumps('selectCamera("cam1"); state.evidenceView="compact"; renderTracks(true);') + ')')
                shot('compact-evidence.png')
                async_js('''const done = arguments[arguments.length - 1]; window.eval(`(async () => { await openDrawer(visibleTracks()[0]); return true; })()`).then(done).catch(e => done(String(e)));''')
                shot('drawer.png')
                js('return window.eval(' + json.dumps('localStorage.setItem("operator.lastActive", String(Date.now()-360000)); state.awayChecked=false; state.incidents[0].opened_utc=new Date().toISOString(); showAwaySummary();') + ')')
                shot('away-summary.png')
                js('return window.eval(' + json.dumps('closeDrawer(); closeAssistant(false); $("rules-panel").hidden=false; $("rules-toggle").setAttribute("aria-expanded","true"); state.rules={rules:[{rule_id:"rz",name:"person zone",class_label:"person",stream_id:"cam1",min_confidence:.6,cooldown_seconds:60,dwell_seconds:45,active_hours:{start:"22:00",end:"06:00",timezone:Intl.DateTimeFormat().resolvedOptions().timeZone},zone:{points:[[.1,.2],[.8,.2],[.7,.75]]},enabled:true,speak:true}],classes:["person","car"],streams:state.streams.map(s=>s.stream_id)}; renderRules(); $("rule-camera").value="cam1"; state.zoneDraft={points:[[.1,.2],[.8,.2],[.7,.75]]}; state.zoneBackup={points:[[.1,.2],[.8,.2],[.7,.75]]}; updateZoneSummary(); $("zone-editor").hidden=false; drawZoneDraft();') + ')')
                shot('rule-zone-editor.png')
                zone = js('return window.eval(' + json.dumps('(() => { const r = $("zone-stage").getBoundingClientRect(), v = $("video").getBoundingClientRect(); const big = getComputedStyle($("zone-editor")).position === "fixed" && r.width > v.width * 1.3; const svg = $("zone-svg"), r2 = svg.getBoundingClientRect(), n = state.zoneDraft.points.length; const opts = {bubbles: true, clientX: r2.left + r2.width * .9, clientY: r2.top + r2.height * .9}; svg.dispatchEvent(new PointerEvent("pointerdown", opts)); const drew = state.zoneDraft.points.length === n + 1 && !$("rules-panel").hidden && !$("zone-editor").hidden; for (const f of [.5, .6, .7]) svg.dispatchEvent(new PointerEvent("pointerdown", {...opts, clientX: r2.left + r2.width * f})); const capped = state.zoneDraft.points.length === 5 && $("zone-step").dataset.tone === "warn" && $("zone-count").textContent === "5 / 5 corners" && $("zone-handles").children.length === 5; const zb = $("zone-toolbar").querySelectorAll("button"); const tappable = [...zb].every(b => b.getBoundingClientRect().height >= 48) && zb[0].id === "zone-cancel" && zb[zb.length - 1].id === "zone-done"; closeRules(false); const restored = state.zoneDraft?.points?.length === 3; return {big, drew, capped, tappable, restored, closed: $("zone-editor").hidden}; })()') + ')')
                assert all(zone.values()), zone
                (args.screenshots / 'operator-welcome.png').write_bytes(base64.b64decode(request('GET', base + '/screenshot')))
                js('return window.eval(' + json.dumps('state.assistantContext={text:"What can you do?",payload:{}}; renderAssistant({intent:"application_help",answer_text:"I can search recorded tracks, answer mission questions and explain general topics. I cannot see the live video.",errors:[]}); archiveAssistantTurn(); state.assistantContext={text:"Why is the sky blue?",payload:{}}; $("assistant-mode").value="general"; renderAssistant({intent:"general_chat",answer_text:"Air scatters blue sunlight more strongly than red light, so the daytime sky looks blue.",conversation:{turns:1},errors:[]});') + ')')
                (args.screenshots / 'conversation.png').write_bytes(base64.b64decode(request('GET', base + '/screenshot')))
            print('PASS: cameras panel add/remove (no limit), WHEP recovery/backoff/cleanup/switch races, compact in-panel search without mode tabs, alphabetical types, session/custom ranges, question correction, images-only results, fixed Search/paging controls, typed history overlay/paging, evidence Older/Newer pages, stable evidence/focus/scroll, pending updates, reconnect pause/All, diagnostics, chart axes/gaps, stale readings, drawer, native video fullscreen, model placement/detail/failure states, filter failure recovery, repeated crop retry, 200px minimum video and in-view overlay panels at 1280x800 and 1280/1024 widths with 714/682/600/540 heights')
        finally:
            if session:
                request('DELETE', f'/session/{session}')
            process.terminate()
            process.wait(timeout=10)
            server.shutdown()


if __name__ == '__main__':
    main()
