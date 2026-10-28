# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Seeded HTTP-boundary mutation fuzzing; run only via make test-fuzz (SDL504)."""
import argparse
import asyncio
import base64
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from urllib.parse import quote

import httpx
from app.main import create_app
from app.core import load_config
from app.adapters import Services
from app.assistant import Assistant, ServiceUnavailable
from app.mission import MissionService
from app.placement import Placement
from app.speech import Arbiter
from app.telemetry import Telemetry
from app.voice import MAX_AUDIO_BYTES
from test_backend import _camera_state

# These are actual UI request shapes; most handlers accept Request rather than
# typed models, so OpenAPI alone cannot generate meaningful JSON mutations.
BODIES = {
    '/api/cameras': {'display_name': 'Fuzz camera', 'source': {'kind': 'publish'}},
    '/api/cameras/{stream_id}': {'display_name': 'Renamed'},
    '/api/rules': {'stream_id': 'cam1', 'class_label': 'person'},
    '/api/rules/{rule_id}': {'cooldown_seconds': 30},
    '/api/incidents/{incident_id}/{action}': {'minutes': 5},
    '/api/speech/{speech_id}/playback': {'event': 'ended'},
    '/api/speech/talk': {'held': False},
    '/api/speech/replay': {'text': 'Test answer.'},
    '/api/mission/ask': {'text': 'What is the mission?'},
    '/api/ask': {'text': 'How many people?', 'conversation_id': 'a' * 32},
}
VALUES = [None, True, False, 0, -1, 2**63, 1.5, '', '\x00', '../' * 8,
          "' OR 1=1 --", '<script>alert(1)</script>', '☃', 'x' * 4097,
          [], {}, ['person'], {'nested': {'value': 1}}]
RAW = [b'', b'{', b'null', b'[]', b'false', b'123', b'"text"',
       b'\xff', b'{"text":"\xff"}', b'[' * 10000 + b']' * 10000]
EXCLUDED = {'/api/stream': 'Long-lived SSE stream; needs a separate streaming/disconnect campaign'}


def unavailable(_):
    raise ServiceUnavailable('External inference is intentionally unavailable in fuzz isolation')


@contextmanager
def target():
    """Real handlers/state; temporary storage and stubbed external services, no workers."""
    with tempfile.TemporaryDirectory(prefix='handheld-fuzz-') as tmp:
        root = Path(tmp)
        state, mediamtx = _camera_state(tmp)
        cfg = load_config(str(root / 'absent.yaml'))
        cfg.update(state.cfg)
        cfg['services'] = {name: f'http://{name}.invalid' for name in
                           ('chatqna', 'audio_analyzer', 'tts', 'dlsps', 'mediamtx', 'alert_agent', 'metrics')}
        cfg.update(crop_dir=str(root / 'crops'), mission_sources_dir=str(root / 'mission'),
                   detector_metadata_path=str(root / 'metadata.yaml'),
                   mission_max_upload_mb=1, timeout_seconds=0.1)
        (root / 'metadata.yaml').write_text('names: [person, car]\n')
        state.cfg = cfg
        state.services.client.close()
        services = Services(cfg['services'], 0.1)
        services.client.close()
        services.client = httpx.Client(transport=httpx.MockTransport(
            lambda req: mediamtx(req) if req.url.host == 'mediamtx.invalid'
            else httpx.Response(503, json={'detail': 'isolated upstream'})))
        state.services = services
        state.db = state.ingest.conn
        state.clear_lock = threading.Lock()
        state.arbiter = Arbiter(lambda text: b'RIFF' + b'\x00' * 40)
        state.alerts.arbiter = state.arbiter
        state.assistant = Assistant(cfg, services, generate=unavailable)
        state.mission = MissionService(cfg, services, generate=unavailable)
        state.assistant.mission = state.mission
        state.placement = Placement(services, cfg)
        state.telemetry = Telemetry(services, 'fuzz')
        state.voice_warmed = float('-inf')
        app = create_app()
        for key, value in vars(state).items():
            setattr(app.state, key, value)
        try:
            yield app
        finally:
            state.alerts.close()
            state.ingest.conn.close()
            services.close()


def cases(method, template, operation, rng, mutations, rule_id='missing'):
    ids = {'stream_id': 'cam1', 'track_id': '1', 'candidate_id': 'missing',
           'rule_id': rule_id, 'incident_id': 'missing', 'match_id': 'missing',
           'speech_id': 'missing', 'action': 'snooze'}
    path = re.sub(r'{([^}]+)}', lambda m: ids[m[1]], template)
    query = {'conversation_id': 'a' * 32} if template == '/api/assistant/reset' else {}
    seed = BODIES.get(template, {})
    body = json.dumps(seed).encode() if method in ('POST', 'PATCH') else b''
    yield 'baseline', path, query, body, 'application/json', False
    for param in operation.get('parameters', []):
        name, location = param['name'], param['in']
        if location not in ('query', 'path'):
            continue
        for value in ['-1', str(2**100), 'NaN', '\x00', "' OR 1=1 --", 'x' * 4097, 'false', '']:
            q = dict(query); p = path
            if location == 'query':
                q[name] = value
            else:
                p = re.sub(r'{([^}]+)}', lambda m: quote(value if m[1] == name else ids[m[1]], safe=''), template)
            yield 'parameter:' + name, p, q, body, 'application/json', False
    if method not in ('POST', 'PATCH', 'PUT'):
        return
    if template == '/api/mission/documents':
        for filename in ['x.pdf', '../x.docx', 'x.txt', 'x\x00.pdf']:
            payload = (f'--fuzz\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode() + b'not a document\r\n--fuzz--\r\n'
            yield 'invalid-upload', path, query, payload, 'multipart/form-data; boundary=fuzz', True
        payload = (b'--fuzz\r\nContent-Disposition: form-data; name="files"; filename="large.pdf"\r\n\r\n'
                   + b'x' * (1024 * 1024 + 1) + b'\r\n--fuzz--\r\n')
        yield 'oversized-upload', path, query, payload, 'multipart/form-data; boundary=fuzz', True
    for raw in RAW:
        yield 'malformed-json', path, query, raw, 'application/json', template in BODIES
    if template in BODIES:
        for key in seed:
            for value in VALUES:
                changed = dict(seed); changed[key] = value
                yield 'field:' + key, path, query, json.dumps(changed).encode(), 'application/json', False
        for _ in range(mutations):
            changed = dict(seed)
            key = rng.choice(list(seed))
            changed[key] = rng.choice(VALUES)
            if rng.random() < .25:
                changed['unexpected'] = rng.choice(VALUES)
            raw = bytearray(json.dumps(changed).encode())
            for _ in range(rng.randint(1, 4)):
                if raw:
                    raw[rng.randrange(len(raw))] = rng.randrange(256)
            yield 'byte-mutation', path, query, bytes(raw), rng.choice(['application/json', 'text/plain', 'application/octet-stream']), False
    if template == '/api/voice/transcribe':
        for raw in [b'RIFF', b'RIFF' + bytes(rng.randrange(256) for _ in range(128)), b'x' * (MAX_AUDIO_BYTES + 1)]:
            yield 'invalid-audio', path, query, raw, 'audio/wav', True


async def campaign(output, seed, mutations):
    rng = random.Random(seed)
    output = output / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + f'-seed-{seed}')
    with target() as app:
        schema = app.openapi()
    output.mkdir(parents=True, exist_ok=True)
    (output / 'SDL504-Perform-REST-API-Fuzz-Testing-OpenAPI.json').write_text(json.dumps(schema, indent=2))
    results = []; coverage = []; count = 0
    log_path = output / 'SDL504-Perform-REST-API-Fuzz-Testing-Requests.jsonl'
    with log_path.open('w') as log:
        for template, methods in schema['paths'].items():
            for method, operation in methods.items():
                if method not in ('get', 'post', 'patch', 'delete', 'put'):
                    continue
                label = method.upper() + ' ' + template
                if template in EXCLUDED:
                    coverage.append({'operation': label, 'status': 'excluded', 'reason': EXCLUDED[template]})
                    continue
                statuses = Counter(); failures = 0; routed = 0
                # Reset application storage for each operation, retain request order
                # within it; the JSONL is an exact replayable sequence.
                with target() as app:
                    rule_id = app.state.alerts.create(app.state.db, {'stream_id': 'cam1', 'class_label': 'person'})['rule_id']
                    scopes = []
                    async def observed(scope, receive, send):
                        scopes.append(scope)
                        await app(scope, receive, send)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=observed, raise_app_exceptions=False),
                                                 base_url='http://fuzz.invalid') as client:
                        for kind, path, query, body, content_type, must_reject in cases(method.upper(), template, operation, rng, mutations, rule_id):
                            count += 1
                            record = {'case': count, 'operation': label, 'kind': kind, 'method': method.upper(),
                                      'path': path, 'query': query, 'body_base64': base64.b64encode(body).decode(),
                                      'content_type': content_type}
                            start = time.monotonic(); problem = None
                            try:
                                response = await asyncio.wait_for(client.request(method, path, params=query,
                                    content=body, headers={'content-type': content_type}), timeout=3)
                                status = response.status_code; statuses[str(status)] += 1
                                route = getattr(scopes[-1].get('route'), 'path', None)
                                routed += route == template
                                record.update(status=status, matched_route=route, response=response.text[:512])
                                if status >= 500 and status not in (502, 503, 504):
                                    problem = 'unexpected_server_error'
                                elif must_reject and 200 <= status < 300:
                                    problem = 'invalid_input_accepted'
                                elif 'Traceback (most recent call last)' in response.text:
                                    problem = 'traceback_disclosure'
                            except Exception as exc:
                                problem = type(exc).__name__; record['exception'] = str(exc)
                            record.update(elapsed_ms=round((time.monotonic()-start)*1000, 2), finding=problem)
                            log.write(json.dumps(record) + '\n'); scopes.clear()
                            if problem:
                                failures += 1; results.append(record)
                    assert app.state.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                coverage.append({'operation': label, 'status': 'tested', 'matched_requests': routed,
                                 'responses': dict(statuses), 'findings': failures})
                print(label, dict(statuses), 'findings', failures, flush=True)
    summary = {'tool': 'handheld seeded API mutation fuzzer v1', 'seed': seed, 'mutations_per_body': mutations,
               'requests': count, 'findings': len(results), 'coverage': coverage,
               'completed_utc': datetime.now(timezone.utc).isoformat(),
               'source_sha256': {str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in [Path(__file__), *sorted((Path(__file__).parent/'app').glob('*.py'))]},
               'limits': ['ASGI application boundary, not proxy/TLS/browser or wire HTTP parser',
                          'External services stubbed; background workers not started',
                          '502/503/504 may represent unavailable stubs and are recorded, not success assertions',
                          'No long-running SSE, concurrency, resource-exhaustion or upstream-image fuzzing',
                          'Camera/rule fixtures populated; no real tracks, incidents, speech playback or mission corpus',
                          'Seeded mutations, not RESTler; bounded run is not exhaustive SDL504 closure']}
    (output/'SDL504-Perform-REST-API-Fuzz-Testing-Summary.json').write_text(json.dumps(summary, indent=2))
    (output/'SDL504-Perform-REST-API-Fuzz-Testing-Findings.json').write_text(json.dumps(results, indent=2))
    print(json.dumps({'requests': count, 'findings': len(results), 'seed': seed, 'evidence': str(output)}))
    assert all(c['matched_requests'] > 0 for c in coverage if c['status'] == 'tested'), 'Operation was not reached'
    return bool(results)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seed', type=int, default=504)
    parser.add_argument('--mutations', type=int, default=32)
    args = parser.parse_args()
    if not 0 <= args.mutations <= 10000:
        parser.error('--mutations must be between 0 and 10000')
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(campaign(args.output, args.seed, args.mutations)))
