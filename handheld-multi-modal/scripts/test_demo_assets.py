#!/usr/bin/env python3
"""Offline check: completed jobs only, cache integrity, failed/partial jobs, video hashes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

import demo_assets as assets


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        calls = []
        def api(path, body=None):
            calls.append(path)
            if path.startswith('/models/download?'):
                assert body == assets.REQUEST
                subdir = path.split('download_path=')[1]
                model = root / 'data/model-download' / subdir / 'ultralytics/public/yolo11s/INT8'
                model.mkdir(parents=True)
                (model / 'yolo11s.xml').write_text('<net><rt_info><nncf><quantization/></nncf></rt_info></net>')
                (model / 'yolo11s.bin').write_bytes(b'test INT8 weights')
                (model / 'metadata.yaml').write_text('description: Ultralytics YOLO11s\nargs:\n  data: coco8.yaml\n')
                return {'job_ids': ['00000000-0000-4000-8000-000000000001']}
            return {'status': 'completed', 'model_name': 'yolo11s', 'hub': 'ultralytics',
                    'result': {'success': True, 'int8_requested': True}}
        fixture_hashes = {
            'yolo11s.xml': hashlib.sha256(b'<net><rt_info><nncf><quantization/></nncf></rt_info></net>').hexdigest(),
            'yolo11s.bin': hashlib.sha256(b'test INT8 weights').hexdigest(),
        }
        with patch.object(assets, 'api', api), patch.object(assets, 'APPROVED_HASHES', (fixture_hashes,)):
            assert not assets.verify_model(root / assets.MODEL_DIR)
            assets.download_model(root, 10)
            assert assets.verify_model(root / assets.MODEL_DIR)
            # A coherent self-generated receipt must not approve new model weights.
            model = root / assets.MODEL_DIR
            receipt = json.loads((model / 'download.json').read_text())
            (model / 'yolo11s.bin').write_bytes(b'future upstream checkpoint')
            receipt['files']['yolo11s.bin'] = assets.sha(model / 'yolo11s.bin')
            (model / 'download.json').write_text(json.dumps(receipt))
            try:
                assets.verify_model(model)
            except ValueError:
                pass
            else:
                raise AssertionError('self-approved model swap accepted')
            (model / 'yolo11s.bin').write_bytes(b'test INT8 weights')
            count = len(calls)
            assets.download_model(root, 10)
            assert len(calls) == count, 'cache hit must not submit another job'
            (root / assets.MODEL_DIR / 'yolo11s.bin').write_bytes(b'corrupt')
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
        # Dry runs must never run the recursive downloader or create assets.
        dry_root = root / 'dry-run'
        dry_run = subprocess.run(
            ['make', '-n', '-C', str(assets.REPO), 'demo', f'ASSET_ROOT={dry_root}'],
            check=True, capture_output=True, text=True).stdout
        assert not dry_root.exists()
        assert '--profile assets stop' in dry_run
        # Exercise the actual Make recipe with different device groups, without Docker.
        group_root = root / 'groups'
        (group_root / 'config').mkdir(parents=True)
        subprocess.run(['make', '--no-print-directory', '-C', str(assets.REPO), 'demo-setup',
                        '-o', 'demo-preflight', '-o', 'demo-assets', '-o', 'tls',
                        f'ASSET_ROOT={group_root}', 'RENDER_GID=12345', 'ACCEL_GID=23456',
                        'HOST_IP=192.0.2.10', 'COTURN_EXTERNAL_IP=192.0.2.10'], check=True)
        groups = [json.loads(line.split('!override ')[1]) for line in (group_root / 'config/devices.yaml').read_text().splitlines() if '!override' in line]
        assert groups == [list(dict.fromkeys(['12345', '23456', str(os.getgid())])),
                          list(dict.fromkeys(['12345', str(os.getgid())]))]
        assert '192.0.2.10' in (group_root / 'config/mediamtx.yml').read_text()
    print('PASS: stock job request, atomic installation, cached reuse, corruption/failed-job rejection, timeout cancellation, video integrity, Make dry-run safety/full-stack failure cleanup, separate device groups, interrupted Chat export recovery')


if __name__ == '__main__':
    main()
