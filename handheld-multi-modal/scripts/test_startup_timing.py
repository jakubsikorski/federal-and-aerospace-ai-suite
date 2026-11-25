#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Offline startup timing, stock log boundaries and Make integration checks."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import startup_timing as timing

REPO = Path(__file__).resolve().parents[1]


class TestStartupTiming(unittest.TestCase):
    def test_report_groups_services_and_sums_model_downloads_without_parent_totals(self):
        def step(group, label, start, finish, status="ok"):
            return timing.interval(group, label, f"2026-11-24T10:00:{start:02d}Z",
                                   f"2026-11-24T10:00:{finish:02d}Z", "fixture", status)

        steps = [
            step("container_download", "intel/chatqna:core_test", 0, 10),
            step("container_download", "intel/audio-analyzer:test", 0, 20),
            step("model_download", "Chat Q&A: model/a snapshot acquisition", 20, 30),
            step("model_download", "Chat Q&A: model/b snapshot acquisition", 30, 40),
            step("model_download", "YOLO26s: weights acquisition and PyTorch loading", 25, 30),
            step("model_packages", "YOLO26s: pip installation script phase", 20, 25),
            step("model_preparation", "YOLO26s downloader startup and model preparation", 0, 50),
            step("model_combined", "SpeechT5: model/vocoder download and OpenVINO export", 20, 50),
            step("backend_build", "Build Backend image", 10, 30),
            step("container_download", "python:3.12-slim (Backend base)", 0, 5),
            step("container_download", "bluenviron/mediamtx:1.21.3-ffmpeg", 5, 10),
            step("readiness", "MediaMTX", 10, 11),
            step("startup", "Start stock application containers", 10, 12),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            report = timing.write_report(directory, "demo-up", "2026-11-24T10:00:00Z",
                                         "2026-11-24T10:01:00Z", 60, 0, steps, [])
            text = (directory / "report.md").read_text()
            stored = json.loads((directory / "report.json").read_text())
            self.assertTrue(all(step["id"] for step in stored["steps"]))
            by_id = {step["id"]: step for step in stored["steps"]}
            self.assertTrue(all(step["parent_id"] in by_id for step in stored["steps"] if step["parent_id"]))
            self.assertTrue(any(step["parent_id"] and by_id[step["parent_id"]]["group"] == "model_preparation"
                                for step in stored["steps"] if step["group"] == "model_download"))
        self.assertIn("**Result:** Ready after **1m 00.0s**", text)
        self.assertIn("| Model downloads | 25.0 s **(partial)** |", text)
        self.assertIn("| Container images | 40.0 s |", text)
        self.assertIn("These rows add up to the overall startup time", text)
        self.assertIn("## Work totals across services", text)
        self.assertIn("### Chat Q&A", text)
        chat = text.split("### Chat Q&A\n", 1)[1].split("### ", 1)[0]
        self.assertIn("Answers questions using the language and document models.", chat)
        self.assertIn("**Separately timed model downloads (sum):** 20.0 s.", chat)
        self.assertIn("| Download model: model/a | 10.0 s |", chat)
        self.assertIn("| Download model: model/b | 10.0 s |", chat)
        self.assertNotIn("YOLO26s", chat)
        self.assertNotIn("<details>", text)
        self.assertIn("**Partial totals:**", text)
        self.assertIn("A child row is included in its parent total.", text)
        detector = text.split("### Model Download / YOLO26s\n", 1)[1].split("### ", 1)[0]
        self.assertIn("| Other initialization / unmeasured phases | 38.0 s |", detector)
        self.assertIn("| **Preparation total** | **50.0 s** |", detector)
        self.assertNotIn("### Shared startup / host configuration", text)
        self.assertEqual(text.count("Start stock application containers"), 1)
        self.assertNotIn("| Step | Group |", text)
        self.assertNotIn("stock-log-boundary", text)
        self.assertEqual([item["service"] for item in report["steps"]],
                         ["chatqna", "audio-analyzer", "chatqna", "chatqna", "model-downloader",
                          "model-downloader", "model-downloader", "tts", "backend", "backend",
                          "demo-cameras", "mediamtx", "shared"])

    def test_report_surfaces_failures_caches_and_service_specific_gaps(self):
        start, finish = "2026-11-24T10:00:00Z", "2026-11-24T10:00:10Z"
        steps = [
            timing.interval("container_download", "nginx:1.30.6-alpine", start, finish, "test", "failed"),
            timing.interval("backend_build", "Backend image matches sources", start, start, "test", "cached"),
            {"group": "model_download", "label": "Chat Q&A: a/b snapshot acquisition",
             "started_at": start, "ended_at": None, "seconds": None, "status": "unavailable",
             "method": "missing-completion-marker"},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            timing.write_report(directory, "demo-up", start, finish, 10, 2, steps,
                                ["audio-analyzer: no recognized model phase markers; internal split unavailable."])
            text = (directory / "report.md").read_text()
        self.assertIn("**Result:** Failed after **10.0 s** (exit 2)", text)
        self.assertIn("1 failed.", text)
        self.assertIn("| Backend image matches sources | Cached |", text)
        self.assertIn("| Backend build | Cached / reused |", text)
        self.assertIn("| Download model: a/b | Not measured |", text)
        self.assertIn("| Model downloads | Not measured **(partial)** |", text)
        self.assertIn("**Attention:** a step failed", text)
        audio = text.split("### Audio Analyzer / Whisper\n", 1)[1].split("### ", 1)[0]
        self.assertIn("**Not measured:**", audio)
        self.assertIn("download and conversion times are unavailable", audio)

    def test_only_combined_conversion_does_not_make_download_total_partial(self):
        steps = [
            timing.interval("model_download", "Chat Q&A: model/a snapshot acquisition",
                            "2026-11-24T10:00:00Z", "2026-11-24T10:00:10Z", "test"),
            timing.interval("model_combined", "Chat Q&A: model/a export and model initialization/loading",
                            "2026-11-24T10:00:10Z", "2026-11-24T10:00:20Z", "test"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            timing.write_report(directory, "demo-up", "2026-11-24T10:00:00Z",
                                "2026-11-24T10:01:00Z", 60, 0, steps, [])
            text = (directory / "report.md").read_text()
        self.assertIn("| Model downloads | 10.0 s |", text)
        self.assertIn("| Model conversion | Not measured **(partial)** |", text)
        self.assertIn("| Combined model preparation | 10.0 s |", text)

    def test_service_download_total_distinguishes_cached_and_missing_work(self):
        start, finish = "2026-11-24T10:00:00Z", "2026-11-24T10:00:10Z"
        measured = timing.interval("model_download", "Chat Q&A: model/a snapshot acquisition",
                                   start, finish, "test")
        cached = timing.interval("model_download", "Chat Q&A: model/b snapshot acquisition",
                                 start, start, "test", "cached")
        missing = {"group": "model_download", "label": "Chat Q&A: model/b snapshot acquisition",
                   "started_at": start, "ended_at": None, "seconds": None, "status": "unavailable",
                   "method": "missing-completion-marker"}
        for other, expected in ((cached, "10.0 s"), (missing, "10.0 s (partial)")):
            with self.subTest(status=other["status"]), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                timing.write_report(directory, "demo-up", start, finish, 10, 0, [measured, other], [])
                text = (directory / "report.md").read_text()
                self.assertIn(f"**Separately timed model downloads (sum):** {expected}.", text)

    def test_elapsed_partition_counts_nested_and_parallel_work_once(self):
        def step(group, label, start, finish, method="monotonic-clock"):
            return timing.interval(group, label, f"2026-11-24T10:00:{start:02d}Z",
                                   f"2026-11-24T10:00:{finish:02d}Z", method)

        steps = [
            step("model_preparation", "YOLO26s downloader startup and model preparation", 0, 10),
            step("setup", "Asset permissions", 1, 2),
            step("startup", "Start Model Download container", 2, 3),
            step("readiness", "Model Download (ultralytics, port 8200)", 3, 5),
            step("container_download", "image/a:1", 10, 20),
            step("container_download", "image/b:1", 15, 25),
            step("backend_build", "Build Backend image", 22, 30),
            step("readiness", "Chat Q&A neural-chat model", 30, 35),
            step("model_download", "Chat Q&A: model/a snapshot acquisition", 20, 34, "stock-log-boundary"),
            step("service_startup", "chatqna: start to application ready", 20, 35, "docker-state-and-stock-log"),
            {"group": "readiness", "label": "Whisper GPU service", "started_at": "2026-11-24T10:00:35Z",
             "ended_at": None, "seconds": None, "status": "failed", "method": "unfinished-step"},
        ]
        report = {"started_at": "2026-11-24T10:00:00Z", "ended_at": "2026-11-24T10:00:40Z",
                  "overall_seconds": 40, "steps": steps}
        expected = {"model_preparation": 10, "container_download": 12, "parallel": 3,
                    "backend_build": 5, "readiness": 5, "unmeasured": 5}
        self.assertEqual(timing.elapsed_breakdown(report), expected)
        self.assertEqual(sum(timing.rounded_partition(expected, 40).values()), 400)
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            timing.write_report(directory, "demo-up", report["started_at"], report["ended_at"],
                                40, 1, steps, [])
            text = (directory / "report.md").read_text()
            overview = text.split("## Where did the time go?", 1)[1].split("## Work totals", 1)[0]
            rows = [line.split("|")[2].strip().strip("*").removesuffix(" s")
                    for line in overview.splitlines() if line.startswith("| ") and not line.startswith("| Startup")]
            units = [round(float(value) * 10) for value in rows]
            self.assertEqual(sum(units[:-1]), units[-1])
            self.assertEqual(units[-1], 400)
            self.assertEqual(json.loads((directory / "report.json").read_text())["elapsed_breakdown"], expected)
        # A concurrent Backend command is not a child of detector preparation.
        report["steps"].append(step("startup", "Start Backend container", 2, 4))
        expected.update(model_preparation=8, parallel=5)
        self.assertEqual(timing.elapsed_breakdown(report), expected)

    def test_elapsed_partition_uses_monotonic_boundaries_when_wall_clock_moves(self):
        step = timing.interval("backend_build", "Build Backend image",
                               "2026-11-24T10:01:02Z", "2026-11-24T10:01:06Z", "monotonic-clock")
        step.update(monotonic_started_at=102, monotonic_ended_at=106)
        report = {"started_at": "2026-11-24T10:00:00Z", "ended_at": "2026-11-24T10:01:10Z",
                  "monotonic_started_at": 100, "overall_seconds": 10, "steps": [step]}
        self.assertEqual(timing.elapsed_breakdown(report), {"unmeasured": 6, "backend_build": 4})

    def test_rounding_preserves_startup_and_work_sums(self):
        totals = {"setup": 0.14, "startup": 0.14, "unmeasured": 0.14}
        displayed = timing.rounded_partition(totals, 0.42)
        self.assertEqual(sum(displayed.values()), 4)
        for group, units in displayed.items():
            self.assertLess(abs(units / 10 - totals[group]), 0.1)
        steps = [
            timing.interval("video_download", label, "2026-11-24T10:00:00Z",
                            "2026-11-24T10:00:00.14Z", "monotonic-clock")
            for label in ("a.mp4", "b.mp4")
        ]
        self.assertEqual(timing.summary_time(steps), "0.2 s")
        self.assertEqual([timing.step_time(step) for step in steps], ["0.1 s", "0.1 s"])

    def test_service_preparation_subtotal_subtracts_overlap(self):
        parent = timing.interval("service_startup", "chatqna: start to application ready",
                                 "2026-11-24T10:00:00Z", "2026-11-24T10:00:10Z", "test")
        phases = [
            timing.interval("model_download", "Chat Q&A: model/a snapshot acquisition",
                            "2026-11-24T10:00:00Z", "2026-11-24T10:00:08Z", "test"),
            timing.interval("model_download", "Chat Q&A: model/b snapshot acquisition",
                            "2026-11-24T10:00:05Z", "2026-11-24T10:00:10Z", "test"),
        ]
        self.assertEqual(timing.preparation_rows(parent, phases), [
            "| Concurrent phases counted twice (subtract) | -3.0 s |",
            "| **Preparation total** | **10.0 s** |",
        ])
        phases[1]["ended_at"] = "2026-11-24T10:00:11Z"
        self.assertIsNone(timing.preparation_rows(parent, phases))

    def test_render_existing_report_leaves_measurements_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            timing.write_report(directory, "demo-up", "2026-11-24T10:00:00Z",
                                "2026-11-24T10:01:00Z", 60, 0, [], [])
            path = directory / "report.json"
            original = path.read_bytes()
            path.with_suffix(".md").write_text("old report")
            result = subprocess.run(
                [sys.executable, str(REPO / "scripts/startup_timing.py"), "render", str(path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), original)
            self.assertIn("## Where did the time go?", path.with_suffix(".md").read_text())
            self.assertIn("Startup timing report available:", result.stdout)

    def test_group_overlap_cache_and_missing_boundaries(self):
        steps = [
            timing.interval("container_download", "a", "2026-11-24T10:00:00Z",
                            "2026-11-24T10:00:10Z", "test"),
            timing.interval("container_download", "b", "2026-11-24T10:00:05Z",
                            "2026-11-24T10:00:15Z", "test"),
            timing.interval("container_download", "cached", "2026-11-24T10:00:15Z",
                            "2026-11-24T10:00:16Z", "test", "cached"),
        ]
        parser = timing.StockPhases("chatqna")
        parser.feed("2026-11-24T10:00:00Z",
                    "Downloading model Intel/neural-chat-7b-v3-3 from HuggingFace Hub...")
        steps.extend(parser.finish())
        totals = timing.summarize(steps)
        self.assertEqual(totals["container_download"]["summed_seconds"], 20)
        self.assertEqual(totals["container_download"]["active_wall_seconds"], 15)
        self.assertEqual(totals["container_download"]["cached_or_reused_steps"], 1)
        self.assertIsNone(totals["model_download"]["summed_seconds"])
        self.assertEqual(totals["model_download"]["unavailable_steps"], 1)

    def test_stock_pip_weights_and_exports_are_separate(self):
        parser = timing.StockPhases("model-downloader")
        messages = [
            "Syncing base dependencies with UV",
            "SUCCESS: All plugin venvs created",
            "Activating virtual environment in /home/appuser/.virtualenvs/dlstreamer...",
            "Downloading and converting: /opt/models/ultralytics/public/yolo26s",
            "\x1b[34mPyTorch:\x1b[0m starting from 'yolo26s.pt'",
            "Export complete (2.3s)",
            "PyTorch: starting from 'yolo26s.pt'",
            "Export complete (2.4s)",
            "PyTorch: starting from 'yolo26s.pt'",
            "Export complete (8.5s)",
        ]
        for index, message in enumerate(messages):
            parser.feed(f"2026-11-24T10:00:{index:02d}Z", message)
        steps = parser.finish()
        self.assertEqual([step["group"] for step in steps],
                         ["model_packages", "model_packages", "model_download",
                          "model_transformation", "model_transformation", "model_transformation"])
        self.assertIn("INT8 with calibration acquisition", steps[-1]["label"])
        self.assertTrue(all(step["seconds"] == 1 for step in steps))

    def test_chat_exports_and_embedded_downloads_are_not_fabricated(self):
        parser = timing.StockPhases("chatqna")
        parser.feed("2026-11-24T10:00:00Z", "Converting a/b model to OpenVINO toolkit format...")
        parser.feed("2026-11-24T10:00:02Z", "Converting c/d model to OpenVINO toolkit format...")
        parser.feed("2026-11-24T10:00:09Z", "Application startup complete.")
        steps = parser.finish()
        self.assertEqual(steps[0]["group"], "model_transformation")
        self.assertEqual(steps[0]["seconds"], 2)
        self.assertEqual(steps[1]["group"], "model_combined")
        self.assertEqual(steps[1]["seconds"], 7)
        for service, start, finish in [
            ("audio-analyzer", "utils.ensure_model: Exporting whisper-medium",
             "utils.ensure_model: Export successful"),
            ("tts", "Exporting SpeechT5 model microsoft/speecht5_tts to OpenVINO IR",
             "Using cached OpenVINO TTS model at models/openvino"),
        ]:
            parser = timing.StockPhases(service)
            parser.feed("2026-11-24T10:00:00Z", start)
            parser.feed("2026-11-24T10:00:04Z", finish)
            steps = parser.finish()
            self.assertEqual(len(steps), 1)
            self.assertEqual(steps[0]["group"], "model_combined")
            self.assertEqual(steps[0]["seconds"], 4)

    def test_raw_logs_and_tokens_are_not_retained(self):
        parser = timing.StockPhases("model-downloader")
        entry = timing.log_entry(
            '2026-11-24T10:00:00.123456789Z {"event":"Starting download: '
            "{'HF_TOKEN': 'sensitive-fixture'}\"}")
        parser.feed(*entry)
        self.assertEqual(parser.finish(), [])
        self.assertIsNone(timing.log_entry("not a timestamp"))
        self.assertIsNone(timing.log_entry("2026-11-24T10:00:00Z {partial"))

    def test_cached_images_not_pulled_and_backend_not_pulled(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, STARTUP_TIMING_DIR=temporary), \
                patch.object(timing.subprocess, "check_output",
                             return_value="a:1\nb:2\na:1\nhandheld/backend:dev\n"):
            def command(args, **_kwargs):
                code = 1 if args[:3] == ["docker", "image", "inspect"] and args[-1] == "b:2" else 0
                return subprocess.CompletedProcess(args, code)
            with patch.object(timing.subprocess, "run", side_effect=command) as run:
                self.assertEqual(timing.pull_images(["docker", "compose", "config", "--images"]), 0)
            pulls = [call.args[0] for call in run.call_args_list if call.args[0][1] == "pull"]
            self.assertEqual(pulls, [["docker", "pull", "b:2"]])
            steps = timing.read_steps(Path(temporary) / "events.jsonl", False)
            self.assertEqual({step["status"] for step in steps}, {"ok", "cached"})
            for step in steps:
                self.assertEqual(step["seconds"], step["monotonic_ended_at"] - step["monotonic_started_at"])

    def test_failed_run_keeps_report_and_notification(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, ASSET_ROOT=temporary), \
                patch.object(timing, "observe_services", return_value=([], ["logs unavailable"])):
            protected = Path(temporary) / "data"
            protected.mkdir(mode=0o500)
            output = io.StringIO()
            with redirect_stdout(output):
                code = timing.run("demo-up", [sys.executable, "-c", "raise SystemExit(7)"])
            self.assertEqual(code, 7)
            reports = list((Path(temporary) / "startup-timings").glob("*/report.json"))
            self.assertEqual(len(reports), 1)
            report = json.loads(reports[0].read_text())
            self.assertEqual(report["returncode"], 7)
            self.assertEqual(report["status"], "failed")
            self.assertIn("monotonic_started_at", report)
            self.assertAlmostEqual(sum(report["elapsed_breakdown"].values()), report["overall_seconds"])
            self.assertIn("Startup timing report available:", output.getvalue())
            self.assertIn("Elapsed startup breakdown (adds up to overall):", output.getvalue())
            self.assertTrue(reports[0].with_suffix(".md").exists())
            self.assertEqual(list(protected.iterdir()), [])
            self.assertEqual(reports[0].parent.stat().st_mode & 0o777, 0o700)

    def test_missing_docker_still_generates_report(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, ASSET_ROOT=temporary), \
                patch.object(timing, "execute", return_value=1), \
                patch.object(timing.subprocess, "run", side_effect=FileNotFoundError("docker")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(timing.run("demo-up", ["make"]), 1)
            report = json.loads(next((Path(temporary) / "startup-timings").glob(
                "*/report.json")).read_text())
            self.assertIn("Docker inspection unavailable", report["warnings"][0])

    def test_make_entrypoint_and_side_effect_free_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = dict(os.environ)
            env.pop("STARTUP_TIMING_DIR", None)
            result = subprocess.run([
                "make", "--no-print-directory", "demo-up", "MAKE=true",
                f"ASSET_ROOT={root / 'run'}",
            ], cwd=REPO, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Startup timing report available:", result.stdout)
            self.assertEqual(len(list((root / "run").glob("startup-timings/*/report.json"))), 1)
            dry = subprocess.run([
                "make", "--no-print-directory", "-n", "demo-up", f"ASSET_ROOT={root / 'dry'}",
            ], cwd=REPO, env=env, text=True, capture_output=True)
            self.assertEqual(dry.returncode, 0, dry.stdout + dry.stderr)
            self.assertFalse((root / "dry").exists())

    def test_make_startup_and_failed_build_without_live_services(self):
        docker = """#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
printf '%s\\n' "$*" >> "$TIMING_COMMAND_LOG"
case "$*" in
  *"image inspect -f"*) echo previous-source-hash ;;
  *"inspect -f"*) echo 'running 2020-01-01T00:00:00Z' ;;
  *"build backend"*) return "${FAKE_BUILD_CODE:-0}" ;;
  *"run --rm --no-deps --entrypoint python3"*) echo cached ;;
esac
"""
        curl = """#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail
case "$*" in
  *"/v3/paths/list"*) echo '{"items":[{"name":"cam1","ready":true},{"name":"cam2","ready":true},{"name":"annotated1","ready":true},{"name":"annotated2","ready":true}]}' ;;
  *"/api/streams"*) echo '{"data":[{"stream_id":"cam1","feed":{"ok":true}},{"stream_id":"cam2","feed":{"ok":true}}]}' ;;
  *"/api/metrics"*) echo '{"data":{"metrics":[{}]}}' ;;
  *"/v1/model-info"*) echo '{"device":"CPU"}' ;;
  *"8888/model"*) echo '{"llm_model":"Intel/neural-chat-7b-v3-3"}' ;;
  *"/health"*) echo '{"status":"ok"}' ;;
  *"8443/"*) echo '<button id="pause-updates">' ;;
  *) echo '{}' ;;
esac
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "commands.bash"
            commands.write_text(
                "# SPDX-FileCopyrightText: (C) 2026 Intel Corporation\n"
                "# SPDX-License-Identifier: Apache-2.0\n"
                "docker() {\n" + "\n".join(docker.splitlines()[4:]) + "\n}\n"
                "curl() {\n" + "\n".join(curl.splitlines()[4:]) + "\n}\n"
                "make() {\n"
                '  if [[ "$*" == *container-images* ]]; then return 0; fi\n'
                '  command make "$@"\n'
                "}\n")
            driver = root / "driver.py"
            driver.write_text(
                "# SPDX-FileCopyrightText: (C) 2026 Intel Corporation\n"
                "# SPDX-License-Identifier: Apache-2.0\n"
                "import sys\nfrom unittest.mock import patch\n"
                f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
                "import startup_timing\n"
                "with patch.object(startup_timing, 'observe_services', return_value=([], [])):\n"
                "    raise SystemExit(startup_timing.run('demo-up', sys.argv[1:]))\n")
            env = dict(os.environ, BASH_ENV=str(commands),
                       ASSET_ROOT=str(root / "assets"), TIMING_COMMAND_LOG=str(root / "commands"))
            for build_code in (0, 5):
                env["FAKE_BUILD_CODE"] = str(build_code)
                env.pop("STARTUP_TIMING_DIR", None)
                result = subprocess.run([
                    sys.executable, str(driver),
                    "make", "--no-print-directory", "demo-up", "-o", "demo-prereq", "-o", "setup",
                    "RENDER_GID=123", "ACCEL_GID=456",
                ], cwd=REPO, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Startup timing report available:", result.stdout)
            reports = list((root / "assets/startup-timings").glob("*/report.json"))
            self.assertEqual(len(reports), 2)
            build_statuses = []
            for path in reports:
                report = json.loads(path.read_text())
                self.assertGreater(report["overall_seconds"], 0)
                self.assertTrue(any(step["group"] == "readiness" for step in report["steps"]))
                build_statuses.extend(step["status"] for step in report["steps"]
                                      if step["group"] == "backend_build")
            self.assertEqual(set(build_statuses), {"ok", "failed"})
            self.assertIn("previously built Backend", result.stderr)


if __name__ == "__main__":
    unittest.main()
