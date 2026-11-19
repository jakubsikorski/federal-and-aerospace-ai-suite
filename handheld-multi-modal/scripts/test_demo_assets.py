#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Offline check: completed jobs only, cache integrity, failed/partial jobs, video hashes."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch

import demo_assets as assets


def check_localhost_only():
    """Localhost-only profile: every host listener and the TURN relay stay on loopback."""
    import yaml
    for name in ('handheld-multi-modal.yaml', 'cameras.yaml'):
        services = yaml.safe_load((assets.REPO / 'compose' / name).read_text()).get('services') or {}
        for service, spec in services.items():
            if spec.get('network_mode') == 'host':
                assert service == 'coturn', f'{name}: {service} uses host networking'
                assert spec['command'] == ['-c', '/etc/coturn/turnserver.conf'], f'{name}: Coturn command overrides config'
            for port in spec.get('ports') or []:
                assert str(port).startswith('127.0.0.1:'), f'{name}: {service} publishes {port} beyond loopback'
    turn = [line.strip() for line in (assets.REPO / 'config/coturn/turnserver.conf').read_text().splitlines()]
    turn = [line for line in turn if line and not line.startswith('#')]
    for required in ('listening-ip=127.0.0.1', 'relay-ip=127.0.0.1', 'denied-peer-ip=0.0.0.0-255.255.255.255',
                     'allowed-peer-ip=127.0.0.1', 'no-tcp-relay', 'no-multicast-peers'):
        assert required in turn, f'turnserver.conf lacks {required}'
    keys = [line.split('=')[0] for line in turn]
    assert keys.count('listening-ip') == keys.count('relay-ip') == keys.count('allowed-peer-ip') == 1
    assert not {'external-ip', 'cli', 'cli-ip'} & set(keys)
    mediamtx = yaml.safe_load((assets.REPO / 'config/mediamtx/mediamtx.yml').read_text())
    assert mediamtx['webrtcAdditionalHosts'] == ['127.0.0.1']
    assert not mediamtx['webrtcIPsFromInterfaces']
    assert [server['url'] for server in mediamtx['webrtcICEServers2']] == ['turn:127.0.0.1:3478?transport=tcp']
    assert all(server.get('clientOnly') for server in mediamtx['webrtcICEServers2'])


def check_speech_device_isolated(root):
    commands = root / 'commands.bash'
    commands.write_text(
        '#!/usr/bin/env bash\n'
        '# SPDX-FileCopyrightText: (C) 2026 Intel Corporation\n'
        '# SPDX-License-Identifier: Apache-2.0\n'
        'set -euo pipefail\n'
        'docker() {\n'
        '  printf "%s\\n" "$*" >> "$COMMAND_LOG"\n'
        '  if [[ "$*" == *" ps --status running --services" ]]; then\n'
        '    printf "%s\\n" "$RUNNING_SERVICES"\n'
        '  fi\n'
        '}\n'
        'curl() { printf \'{"device":"%s"}\\n\' "$TTS_DEVICE"; }\n')
    services = ['coturn', 'mediamtx', 'mqtt', 'metrics', 'dlsps', 'alert-agent',
                'chatqna', 'audio-analyzer', 'tts', 'backend', 'proxy']
    for camera_count in (0, 1, 2):
        deployment = root / f'deployment-{camera_count}'
        (deployment / 'config/tls').mkdir(parents=True)
        for name in ('server.crt', 'server.key'):
            (deployment / 'config/tls' / name).write_text('fixture TLS material\n')
        command = ['make', '--no-print-directory', '-C', str(assets.REPO),
                   f'ASSET_ROOT={deployment}', 'RENDER_GID=12345', 'ACCEL_GID=23456']
        subprocess.run(command + ['setup', '-o', 'tls', 'TTS_DEVICE=CPU',
                                  f'CAMERAS={camera_count or 2}',
                                  f'DEMO_FEEDS={"yes" if camera_count else ""}'],
                       check=True, capture_output=True, text=True)
        devices = deployment / 'config/devices.yaml'
        devices.write_text(devices.read_text() + '  custom:\n    cpus: 2\n')
        (deployment / 'data/backend/history.fixture').write_bytes(b'camera history')
        selection = deployment / 'config/speech/tts.env'
        unchanged = {path: path.read_bytes() for path in deployment.rglob('*')
                     if path.is_file() and path != selection}
        log = root / f'commands-{camera_count}.log'
        env = dict(os.environ, BASH_ENV=str(commands), COMMAND_LOG=str(log),
                   RUNNING_SERVICES='\n'.join(services))
        for device in ('GPU', 'CPU'):
            result = subprocess.run(command + ['speech-device', '-o', 'preflight',
                                               f'TTS_DEVICE={device}', 'CAMERAS=2',
                                               'RENDER_GID=34567', 'ACCEL_GID=45678'],
                                    env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
            assert selection.read_text() == f'TEXT_TO_SPEECH__MODELS__TTS__DEVICE={device}\n'
            assert all(path.read_bytes() == content for path, content in unchanged.items()), \
                'speech-device changed deployment configuration, TLS or history'
            assert {path for path in deployment.rglob('*') if path.is_file()} == set(unchanged) | {selection}
        calls = log.read_text().splitlines()
        assert len(calls) == 4, calls
        assert all(call.endswith(' ps --status running --services') for call in calls[::2]), calls
        assert all(call.endswith(' up -d --no-deps tts') for call in calls[1::2]), calls
        if camera_count == 0:
            for missing in [None, *services]:
                log.unlink()
                env['RUNNING_SERVICES'] = '\n'.join(s for s in services if s != missing) if missing else ''
                before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                          for path in deployment.rglob('*') if path.is_file()}
                result = subprocess.run(command + ['speech-device', '-o', 'preflight',
                                                   'TTS_DEVICE=GPU'],
                                        env=env, capture_output=True, text=True)
                assert result.returncode != 0, 'speech-device accepted a stopped or incomplete application'
                assert 'run make up or make demo-up' in result.stderr, result.stderr
                assert before == {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                  for path in deployment.rglob('*') if path.is_file()}, \
                    'speech-device changed settings before rejecting the stopped application'
                calls = log.read_text().splitlines()
                assert len(calls) == 1 and calls[0].endswith(' ps --status running --services'), calls


def check_live_speech_device(root):
    with patch('ssl.create_default_context'), patch('urllib.request.build_opener'):
        checker = runpy.run_path(str(assets.REPO / 'scripts/check_live.py'))
    namespace = checker['main'].__globals__
    deployment = root / 'live-check'
    (deployment / 'config/speech').mkdir(parents=True)
    selection = deployment / 'config/speech/tts.env'
    # Stale deployment YAML must not override the live speech selection.
    (deployment / 'config/devices.yaml').write_text(
        'TEXT_TO_SPEECH__MODELS__TTS__DEVICE: CPU\n')
    expected_model = checker['configured'](
        assets.REPO / 'config/chatqna/config.yaml', r'^\s*LLM_MODEL_ID:\s*"([^"]+)"')
    for device in ('GPU', 'CPU'):
        calls = []

        def call(url, *args, **kwargs):
            calls.append(url)
            if url.endswith('/api/streams'):
                return {'data': []}
            if url.endswith('/v3/paths/list'):
                return {'items': []}
            if url.endswith('/pipelines/status'):
                return []
            if url.endswith('/api/metrics'):
                return {'data': {'status': 'ok', 'metrics': [{}]}}
            if url.endswith('/model'):
                return {'llm_model': expected_model}
            if url.endswith('/chat'):
                return {'status': 'Success', 'metadata': 'Answer: 4'}
            if url.endswith('/v1/model-info'):
                return {'device': device, 'model': 'microsoft/speecht5_tts'}
            if url.endswith('/v1/audio/speech'):
                return b'RIFF' + b'\0' * 1024
            if url.endswith('/v1/audio/transcriptions'):
                return {'text': 'Camera two is ready.'}
            if url.endswith('/api/v1/health'):
                return {'status': 'healthy'}
            if url.endswith('/health'):
                return {'status': 'ok'}
            if url == 'https://127.0.0.1:8443/':
                return b'<button id="pause-updates">'
            raise AssertionError(f'unexpected live-check request: {url}')

        selection.write_text(f'TEXT_TO_SPEECH__MODELS__TTS__DEVICE={device}\n')
        with patch.dict(namespace, ASSET_ROOT=deployment, call=call, failed=False), \
                contextlib.redirect_stdout(io.StringIO()):
            assert checker['main']() == 0
        assert any(url.endswith('/v1/audio/speech') for url in calls)
        assert any(url.endswith('/v1/audio/transcriptions') for url in calls)
    for contents in (None, '', 'TEXT_TO_SPEECH__MODELS__TTS__DEVICE=NPU\n'):
        if contents is None:
            selection.unlink()
        else:
            selection.write_text(contents)
        calls.clear()
        output = io.StringIO()
        with patch.dict(namespace, ASSET_ROOT=deployment, call=call, failed=False), \
                contextlib.redirect_stdout(output):
            assert checker['main']() == 1
        assert 'missing or invalid config/speech/tts.env' in output.getvalue()
        assert not any(url.endswith('/v1/audio/speech') for url in calls)


def check_asset_recovery_quoting(root):
    commands = root / 'recovery-commands.bash'
    commands.write_text(
        '#!/usr/bin/env bash\n'
        '# SPDX-FileCopyrightText: (C) 2026 Intel Corporation\n'
        '# SPDX-License-Identifier: Apache-2.0\n'
        'set -euo pipefail\n'
        'rm() { return 1; }\n'
        'docker() { return 0; }\n')
    recipe = (
        '.PHONY: test-recovery-command\n'
        'test-recovery-command:\n'
        '\t@ASSET_ROOT="$$RECOVERY_ROOT"\n'
        '\t$(call remove_assets,data config/tls)\n')
    for name in ('handheld data', 'handheld\'"$;*[]\\data'):
        deployment = str(root / name)
        env = dict(os.environ, BASH_ENV=str(commands), RECOVERY_ROOT=deployment)
        result = subprocess.run(
            ['make', '--no-print-directory', '-C', str(assets.REPO),
             '-f', 'Makefile', '-f', '-', 'test-recovery-command'],
            input=recipe, env=env, capture_output=True, text=True, check=True)
        recovery = result.stderr.split('Run: ', 1)[1].strip()
        assert shlex.split(recovery) == [
            'sudo', 'rm', '-rf', '--', deployment + '/data', deployment + '/config/tls',
        ], recovery


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        check_live_speech_device(root)
        check_asset_recovery_quoting(root)
        calls = []
        def api(path, body=None):
            calls.append(path)
            if path.startswith('/models/download?'):
                assert body == assets.REQUEST
                subdir = path.split('download_path=')[1]
                model = root / 'data/model-download' / subdir / 'ultralytics/public/yolo26s/INT8'
                model.mkdir(parents=True)
                (model / 'yolo26s.xml').write_text('<net><rt_info><nncf><quantization/></nncf></rt_info></net>')
                (model / 'yolo26s.bin').write_bytes(b'test INT8 weights')
                (model / 'metadata.yaml').write_text('description: Ultralytics YOLO26s\nargs:\n  data: coco8.yaml\n')
                return {'job_ids': ['00000000-0000-4000-8000-000000000001']}
            return {'status': 'completed', 'model_name': 'yolo26s', 'hub': 'ultralytics',
                    'result': {'success': True, 'int8_requested': True}}
        fixture_hashes = {
            'yolo26s.xml': hashlib.sha256(b'<net><rt_info><nncf><quantization/></nncf></rt_info></net>').hexdigest(),
            'yolo26s.bin': hashlib.sha256(b'test INT8 weights').hexdigest(),
        }
        with patch.object(assets, 'api', api), patch.object(assets, 'APPROVED_HASHES', (fixture_hashes,)):
            assert not assets.verify_model(root / assets.MODEL_DIR)
            assets.download_model(root, 10)
            assert assets.verify_model(root / assets.MODEL_DIR)
            # A coherent self-generated receipt must not approve new model weights.
            model = root / assets.MODEL_DIR
            receipt = json.loads((model / 'download.json').read_text())
            (model / 'yolo26s.bin').write_bytes(b'future upstream checkpoint')
            receipt['files']['yolo26s.bin'] = assets.sha(model / 'yolo26s.bin')
            (model / 'download.json').write_text(json.dumps(receipt))
            try:
                assets.verify_model(model)
            except ValueError:
                pass
            else:
                raise AssertionError('self-approved model swap accepted')
            (model / 'yolo26s.bin').write_bytes(b'test INT8 weights')
            count = len(calls)
            assets.download_model(root, 10)
            assert len(calls) == count, 'cache hit must not submit another job'
            (root / assets.MODEL_DIR / 'yolo26s.bin').write_bytes(b'corrupt')
            try:
                assets.download_model(root, 10)
            except ValueError:
                pass
            else:
                raise AssertionError('corruption was accepted')
            assert len(calls) == count, 'corrupt cache must not be silently overwritten'
        for status in ('failed', 'canceled'):
            failed_root = root / status
            with patch.object(assets, 'api', side_effect=[
                {'job_ids': ['00000000-0000-4000-8000-000000000002']}, {'status': status}]), \
                    patch.object(assets.time, 'sleep'):
                try:
                    assets.download_model(failed_root, 1)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError(f'{status} job was accepted')
                assert not (failed_root / assets.MODEL_DIR).exists()
        with patch.object(assets, 'api', side_effect=[
            {'job_ids': ['00000000-0000-4000-8000-000000000003']}, {'status': 'downloading'}, {}]) as mock:
            try:
                assets.download_model(root / 'timeout', -1)
            except TimeoutError:
                assert mock.call_args.args[0].endswith('/cancel')
            else:
                raise AssertionError('timeout was accepted')
        video = root / 'data/dlsps/demo-video/classroom.avi'
        video.parent.mkdir(parents=True)
        video.write_bytes(b'partial video')
        try:
            assets.download_video(root)
        except ValueError:
            pass
        else:
            raise AssertionError('partial video was accepted')
        assert video.read_bytes() == b'partial video'
        # A failing or wrong first source falls back to the next one; all failing is an error.
        good = b'fallback video'
        sources = ('https://first.invalid/a.mp4', 'https://second.invalid/b.mp4')
        clip = ('fallback.mp4', sources, hashlib.sha256(good).hexdigest())

        def serve(content):
            def fake_urlopen(request, timeout):
                if request.full_url == sources[0]:
                    raise OSError('HTTP Error 403: Forbidden')
                response = MagicMock()
                response.__enter__.return_value = io.BytesIO(content)
                return response
            return fake_urlopen

        with patch.object(assets, 'VIDEOS', (clip,)), \
                patch.object(assets.urllib.request, 'urlopen', serve(good)):
            assets.download_video(root / 'fallback')
        assert (root / 'fallback/data/dlsps/demo-video/fallback.mp4').read_bytes() == good
        with patch.object(assets, 'VIDEOS', (clip,)), \
                patch.object(assets.urllib.request, 'urlopen', serve(b'tampered')):
            try:
                assets.download_video(root / 'tampered')
            except RuntimeError as error:
                assert 'checksum mismatch' in str(error) and '403' in str(error)
            else:
                raise AssertionError('no verified source was accepted')
        assert not (root / 'tampered/data/dlsps/demo-video/fallback.mp4').exists()
        # An interrupted stock Chat export is preserved, then retried; complete ones survive.
        chat = root / 'chat'
        partial = chat / 'Intel/neural-chat-7b-v3-3'
        partial.mkdir(parents=True)
        (partial / 'tokenizer.json').write_text('partial tokenizer')
        complete = chat / 'ibm-granite/granite-embedding-125m-english'
        complete.mkdir(parents=True)
        for name in ('openvino_model.xml', 'openvino_model.bin', 'openvino_tokenizer.xml',
                     'openvino_tokenizer.bin', 'config.json', 'tokenizer_config.json'):
            (complete / name).write_text('complete fixture')
        assets.repair_chat_cache(chat, ['Intel/neural-chat-7b-v3-3',
                                      'ibm-granite/granite-embedding-125m-english', 'absent/model'])
        assert not partial.exists()
        backup = next(partial.parent.glob('*.incomplete-*'))
        assert (backup / 'tokenizer.json').read_text() == 'partial tokenizer'
        assert (complete / 'openvino_model.bin').read_text() == 'complete fixture'
        # Offline start needs every export plus the Hub snapshot that ref points to.
        embed = ['ibm-granite/granite-embedding-125m-english']
        assert not assets.chat_cache_ready(chat, embed)
        repo = chat / 'models--ibm-granite--granite-embedding-125m-english'
        (repo / 'refs').mkdir(parents=True)
        (repo / 'refs/main').write_text('abc123\n')
        assert not assets.chat_cache_ready(chat, embed)
        (repo / 'snapshots/abc123').mkdir(parents=True)
        assert assets.chat_cache_ready(chat, embed)
        assert not assets.chat_cache_ready(chat, embed + ['Intel/neural-chat-7b-v3-3'])
        # Dry runs must never run the recursive downloader or create assets.
        dry_root = root / 'dry-run'
        dry_run = subprocess.run(
            ['make', '-n', '-C', str(assets.REPO), 'demo-up', f'ASSET_ROOT={dry_root}'],
            check=True, capture_output=True, text=True).stdout
        assert not dry_root.exists()
        assert '--profile assets stop' in dry_run
        assert ':/handheld/config/dlsps/detector-profile.json:ro' in dry_run
        # Exercise the actual Make recipe with different device groups, without Docker.
        group_root = root / 'groups'
        (group_root / 'config').mkdir(parents=True)
        subprocess.run(['make', '--no-print-directory', '-C', str(assets.REPO), 'setup',
                        '-o', 'tls',
                        f'ASSET_ROOT={group_root}', 'RENDER_GID=12345', 'ACCEL_GID=23456'], check=True)
        groups = [json.loads(line.split('!override ')[1]) for line in (group_root / 'config/devices.yaml').read_text().splitlines() if '!override' in line]
        assert groups == [list(dict.fromkeys(['12345', '23456', str(os.getgid())])),
                          list(dict.fromkeys(['12345', str(os.getgid())]))]
        assert not (group_root / 'config/mediamtx.yml').exists(), 'MediaMTX needs no per-device LAN copy'
        check_speech_device_isolated(root)
    check_localhost_only()
    # The committed example packet is exactly what the generator produces.
    assert assets.MISSION_EXAMPLE.read_bytes() == assets._pdf_bytes(assets.MISSION_DEMO_LINES), \
        'examples/demo-mission-packet.pdf is stale; run: python3 scripts/demo_assets.py mission'
    dry = subprocess.run(['make', '-n', '--no-print-directory', '-C', str(assets.REPO), 'demo-up'],
                         capture_output=True, text=True).stdout
    assert 'mission/documents' not in dry and 'demo-mission-packet' not in dry, 'demo-up must not load a mission'
    # Deployment without demo feeds never downloads demo videos or starts publishers.
    own = subprocess.run(['make', '-n', '--no-print-directory', '-C', str(assets.REPO), 'up'],
                         capture_output=True, text=True).stdout
    assert 'demo_assets.py model' in own and 'demo_assets.py video' not in own and 'up -d --force-recreate cam' not in own
    assert 'demo_assets.py video' in dry and 'up -d --force-recreate cam1 cam2' in dry
    # Licenses are shown and accepted before anything is pulled; demo starts add the videos.
    for plan, demo in ((own, False), (dry, True)):
        notice = plan.index('scripts/license_notice.py')
        assert notice < plan.index('model-downloader -ec'), 'licenses must be accepted before any download'
        assert ('license_notice.py --demo' in plan) == demo
    print('PASS: stock job request, atomic installation, cached reuse, corruption/failed-job rejection, timeout cancellation, video integrity, Make dry-run safety/full-stack failure cleanup, separate device groups, interrupted Chat export recovery, committed example mission packet not auto-loaded, localhost-only bindings')


@contextlib.contextmanager
def captured_output(capture):
    '''Send this process's stdout and stderr, including output written by
    child code below the Python level, to the `capture` file.'''
    sys.stdout.flush()
    sys.stderr.flush()
    saved_stdout, saved_stderr = os.dup(1), os.dup(2)
    os.dup2(capture.fileno(), 1)
    os.dup2(capture.fileno(), 2)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


def run_quietly():
    '''Run the checks with their output captured. Simulated Model Download
    failures print alarming lines, so the full output is shown only on failure;
    on success only the final PASS line is printed.'''
    with tempfile.TemporaryFile() as capture:
        try:
            with captured_output(capture):
                main()
        except BaseException:
            capture.seek(0)
            sys.stderr.buffer.write(capture.read())
            sys.stderr.flush()
            raise
        capture.seek(0)
        lines = capture.read().decode(errors='replace').strip().splitlines()
        print(lines[-1] if lines else 'PASS')


if __name__ == '__main__':
    if os.environ.get('TEST_VERBOSE') == '1':
        main()
    else:
        run_quietly()
