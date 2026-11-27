#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Offline tests for the shared startup timing tool and its Handheld integration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SHARED = REPO / "tools/startup-timing"
sys.path.insert(0, str(SHARED))
TIMING = SHARED / "timing.py"
HELPERS = SHARED / "timing.sh"

from startup_timing import compare, config, images, journal, logmarkers, render, report  # noqa: E402
from startup_timing import cli  # noqa: E402

HANDHELD = config.load(REPO / "startup-timing.toml")


def step(group, label, offset, seconds, status="ok", **extra):
    """A recorded step as the journal reader produces it, on the run's clock."""
    return {"group": group, "label": label, "offset": offset, "seconds": seconds,
            "status": status, "started_at": "2026-11-24T10:00:00+00:00", **extra}


def build(steps, total=10.0, code=0, warnings=()):
    return report.build("demo", "2026-11-24T10:00:00+00:00", "2026-11-24T10:00:10+00:00",
                        total, code, steps, list(warnings))


def run_tool(script, cwd, *args, env=None):
    """Run a bash script under `timing.sh run`, without any configuration file."""
    environment = {k: v for k, v in os.environ.items() if not k.startswith("STARTUP_TIMING")}
    environment.update(env or {})
    return subprocess.run(["bash", str(HELPERS), "run", *args, "--", "bash", "-c", script],
                          cwd=cwd, env=environment, text=True, capture_output=True)


class TestZeroConfig(unittest.TestCase):
    SCRIPT = f"""source {HELPERS}
timed setup 'Check prerequisites' sleep 0.1
timing_cached models 'Detector model cache'
timed --service backend build 'Build image' bash -c 'source {HELPERS}; timed build "Compile" sleep 0.2'
t=$(timing_begin start 'Wait until ready'); sleep 0.1; timing_end "$t"
sleep 0.1
"""

    def test_helpers_only_no_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_tool(self.SCRIPT, temporary, "--name", "demo")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(list(Path(temporary).glob("*.toml")))
            latest = Path(temporary, "startup-timings", "latest")
            self.assertTrue(latest.is_symlink())
            data = json.loads((latest / "report.json").read_text())
            text = (latest / "report.md").read_text()
        self.assertEqual(data["schema"], 2)
        by_label = {s["label"]: s for s in data["steps"]}
        self.assertEqual(by_label["Compile"]["parent_id"], by_label["Build image"]["id"])
        self.assertEqual(by_label["Build image"]["service"], "backend")
        self.assertEqual(by_label["Detector model cache"]["status"], "cached")
        self.assertIn("**Succeeded in", text)
        self.assertIn("What took longest", text)
        self.assertIn("partly warm", text)
        self.assertIn("| Build image | Backend |", text)  # undeclared names are title-cased
        self.assertIn("↳ Compile", text)
        self.assertIn("*Time with no step running*", text)
        self.assertIn("## Time by service", text)
        self.assertNotIn("## Details", text)  # exact-partition maths stay out of the default view
        self.assertLessEqual(len(result.stdout.strip().splitlines()), 12)
        self.assertIn("Report:", result.stdout)

    def test_summary_json_and_paste_ready_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "app.sh"
            script.write_text(f'source "{HELPERS}"\n'
                              'timed --service db load "Load data" sleep 0.2\n'
                              'timed --service web start "Start web" sleep 0.1\n')
            run = subprocess.run(["bash", str(HELPERS), "run", "--name", "x", "--", "bash", str(script)],
                                 cwd=tmp, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            data = json.loads((Path(tmp) / "startup-timings/latest/summary.json").read_text())
            self.assertEqual(data["schema"], "startup-timing-summary/1")
            self.assertEqual([s["id"] for s in data["services"]], ["db", "web"])
            self.assertEqual(data["bottleneck"]["id"], "db")
            self.assertEqual(data["bottleneck"]["work"][0]["label"], "Load data")
            brief = subprocess.run(["bash", str(HELPERS), "summary"], cwd=tmp, capture_output=True, text=True)
            self.assertIn("## Time by service", brief.stdout)
            self.assertNotIn("<details>", brief.stdout)  # short enough to paste into a cycle report
            self.assertIn("What each service spent its time on", (Path(tmp) / "startup-timings/latest/report.md").read_text())

    def test_failure_and_unfinished_step_still_produce_a_partial_report(self):
        script = (f"source {HELPERS}; timed setup Good true; timed setup Bad false; "
                  "timing_begin setup Dangling >/dev/null; exit 3")
        with tempfile.TemporaryDirectory() as temporary:
            result = run_tool(script, temporary)
            self.assertEqual(result.returncode, 3)
            data = json.loads(Path(temporary, "startup-timings/latest/report.json").read_text())
            text = Path(temporary, "startup-timings/latest/report.md").read_text()
        self.assertEqual((data["status"], data["returncode"]), ("failed", 3))
        dangling = next(s for s in data["steps"] if s["label"] == "Dangling")
        self.assertIsNone(dangling["seconds"])
        self.assertIn("**FAILED (exit 3)", text)
        self.assertIn("| Dangling | Setup | unavailable | failed (no end recorded) |", text)
        self.assertIn("FAILED (exit 3)", result.stdout)

    def test_helpers_are_silent_outside_a_timed_run(self):
        result = subprocess.run(["bash", "-c", f"source {HELPERS}; timed g l echo hi; timing_cached g l"],
                                env={k: v for k, v in os.environ.items() if not k.startswith("STARTUP_TIMING")},
                                text=True, capture_output=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "hi\n", ""))

    def test_compare_two_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            for seconds in ("0.1", "0.4"):
                run_tool(f"source {HELPERS}; timed a Work sleep {seconds}", temporary)
            runs = sorted((p for p in Path(temporary, "startup-timings").iterdir() if p.name != "latest"),
                          key=lambda p: json.loads((p / "report.json").read_text())["started_at"])
            result = subprocess.run(["bash", str(HELPERS), "compare", str(runs[0]), str(runs[1] / "report.json")],
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("| Work |", result.stdout)
        self.assertRegex(result.stdout, r"\| Work \| 0\.\d s \| 0\.\d s \| \+0\.\d s")


class TestFixes(unittest.TestCase):
    def sh(self, script, **env):
        environment = {k: v for k, v in os.environ.items() if not k.startswith("STARTUP_TIMING")}
        environment.update(env)
        return subprocess.run(["bash", "-c", script], env=environment, text=True, capture_output=True)

    def test_helpers_survive_set_e_and_bad_timing_directory(self):
        result = self.sh(f"set -e; source {HELPERS}; timed a b echo ran; echo survived", STARTUP_TIMING_DIR="/nonexistent")
        self.assertEqual((result.returncode, result.stdout), (0, "ran\nsurvived\n"))

    def test_relative_source_and_dashed_labels(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                ["bash", "-c", f"source ./timing.sh; cd /; t=$(timing_begin a --weird); timing_end \"$t\"; "
                 f"timed a -1 true; wc -l < {temporary}/events.jsonl"],
                cwd=SHARED, env={**os.environ, "STARTUP_TIMING_DIR": temporary}, text=True, capture_output=True)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, "4", ""))

    def test_signal_exit_status_is_shell_convention(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_tool("kill -TERM $$", temporary)
        self.assertEqual(result.returncode, 143)
        self.assertIn("exit 143", result.stdout)

    def test_unfinished_step_is_not_idle(self):
        data = build([step("a", "Long", 2.0, None, "failed", method="unfinished-step")], total=10.0, code=130)
        self.assertAlmostEqual(data["idle_seconds"], 2.0)

    def test_parent_with_cached_child_still_leads(self):
        parent = step("a", "Parent", 0, 5.0, id="p")
        child = step("a", "Child", 0, None, "cached", parent_id="p")
        data = build([parent, child, step("a", "Small", 6.0, 0.2)])
        self.assertEqual(render.facts(data)["slowest"]["label"], "Parent")

    def test_render_edges(self):
        self.assertEqual(render.seconds(59.96), "1m 00s")
        self.assertEqual(render.offset(59.96), "+1:00")
        self.assertEqual(config.pretty("gpu_setup"), "Gpu Setup")
        self.assertEqual(config.pretty("API_start"), "API Start")
        text = render.markdown(build([step("a", "A", 0, 0.01)], total=0))
        self.assertNotIn("( of the total)", text)
        self.assertNotIn("()", text)

    def test_all_cached_run_says_nothing_to_measure(self):
        data = build([step("models", "Model cache", 0, None, "cached")], total=0.1)
        text = render.markdown(data, explain_more=True)
        self.assertIn("All steps were cached", text)
        self.assertNotIn("Time with no step running", text)
        self.assertIn("| Models | 1 | cached | cached | 1 | 0 |", text)

    def test_compare_cold_against_warm(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for name, status, seconds in (("a", "ok", 2.0), ("b", "cached", None)):
                path = Path(temporary, name)
                path.mkdir()
                (path / "report.json").write_text(json.dumps(build([step("g", "Work", 0, seconds, status)])))
                paths.append(path)
            text = compare.compare(*paths)
        self.assertIn("A is cold and B is warm", text)
        self.assertIn("now cached", text)


class TestReport(unittest.TestCase):
    def test_sequential_rows_and_idle_add_up_to_total(self):
        data = build([step("a", "One", 0, 2.0), step("b", "Two", 3.0, 4.0)])
        self.assertAlmostEqual(data["idle_seconds"], 4.0)
        self.assertAlmostEqual(data["overlap_seconds"], 0.0)
        text = render.markdown(data)
        self.assertIn("| +3.0 s | Two | B | 4.0 s | ok |", text)
        self.assertIn("| | *Time with no step running* | | 4.0 s | | |", text)
        self.assertNotIn("parallel", text)

    def test_parallel_steps_are_flagged_not_hidden(self):
        data = build([step("a", "One", 0, 6.0, service="a"), step("a", "Two", 2.0, 6.0, service="b")])
        self.assertAlmostEqual(data["overlap_seconds"], 4.0)
        self.assertAlmostEqual(data["idle_seconds"], 2.0)
        self.assertIn("run in parallel", render.markdown(data))

    def test_parallel_downloads_are_one_activity_not_every_services_time(self):
        pulls = [step("container_download", f"image-{n}", 1.0, 90.0, service=f"s{n}", bytes=2_000_000_000)
                 for n in range(4)]
        work = [step("startup", "Start app", 95.0, 5.0, service="s0")]
        data = build(pulls + work, total=100.0)
        text = render.markdown(data)
        self.assertIn("**What took longest:** Container Download, 1m 30s (90% of the run). 4 downloads, 8.0 GB", text)
        table = text.split("## Time by service")[1].split("##")[0]
        self.assertIn("| S0 | 5.0 s |", table)  # its own work, not 90 s of pulling
        self.assertNotIn("S1", table)  # a service that only pulled an image has no row of its own
        self.assertNotIn("Cached, already running", table)
        self.assertIn("Not counted per service: parallel container download", table)
        summary = json.loads(json.dumps(__import__("startup_timing.services", fromlist=["x"]).summary(data, {})))
        self.assertEqual(summary["activities"][0]["parallel"], True)

    def test_nesting_ordering_cold_warm_and_unavailable(self):
        steps = [step("g", "Later", 5.0, 1.0), step("g", "Parent", 0.0, 4.0, id="p"),
                 step("g", "Child", 1.0, 1.0, parent_id="p"),
                 step("g", "Cache hit", 0.0, 0.0, "cached"),
                 step("g", "Lost", 6.5, None, "unavailable")]
        data = build(steps)
        text = render.markdown(data)
        rows = [line for line in text.splitlines() if line.startswith("| +")]
        self.assertEqual([r.split("|")[2].strip() for r in rows],
                         ["Parent", "&nbsp;&nbsp;↳ Child", "Later", "Lost"])
        self.assertIn("partly warm (1 of 5 steps cached)", text)
        self.assertIn("| Lost | G | unavailable | unavailable |", text)
        self.assertNotIn("Lost: 0", text)
        cold = render.markdown(build([step("g", "A", 0, 1.0)]))
        self.assertIn("cold start (nothing cached)", cold)

    def test_explain_and_gantt_are_opt_in(self):
        data = build([step("model_download", "Fetch", 0, 2.0)])
        plain = render.markdown(data)
        full = render.markdown(data, {"groups": {"model_download": "Model downloads"}}, True, True)
        self.assertNotIn("## Details", plain)
        self.assertNotIn("mermaid", plain)
        self.assertIn("| Model downloads | 1 | 2.0 s | 2.0 s | 0 | 0 |", full)
        self.assertIn("```mermaid", full)

    def test_toml_titles_are_optional_polish(self):
        data = build([step("model_download", "Fetch", 0, 2.0, service="chatqna")])
        self.assertIn("| Chatqna |", render.markdown(data))
        titled = render.markdown(data, HANDHELD)
        self.assertIn("| Chat Q&A |", titled)

    def test_old_format_reports_still_render_and_bad_files_fail_clearly(self):
        old = {"target": "demo-up", "started_at": "2026-11-24T10:00:00Z", "ended_at": "2026-11-24T10:01:00Z",
               "overall_seconds": 60, "returncode": 0, "status": "ok", "warnings": [], "groups": {},
               "elapsed_breakdown": {},
               "steps": [{"group": "setup", "label": "Old step", "started_at": "2026-11-24T10:00:10Z",
                          "ended_at": "2026-11-24T10:00:20Z", "seconds": 10.0, "status": "ok",
                          "method": "monotonic-clock", "service": "shared"}]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary, "report.json")
            path.write_text(json.dumps(old))
            before = path.read_bytes()
            result = subprocess.run([sys.executable, str(TIMING), "render", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(path.read_bytes(), before)
            self.assertIn("| +10.0 s | Old step |", path.with_suffix(".md").read_text())
            path.write_text('{"hello": 1}')
            result = subprocess.run([sys.executable, str(TIMING), "render", str(path)],
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a startup timing report", result.stderr)


class TestLogPlugin(unittest.TestCase):
    def test_plugin_only_loads_when_configured(self):
        self.assertEqual(cli.collect_logs({}, "a", "b"), ([], []))
        self.assertTrue(config.uses_log_plugin(HANDHELD))
        self.assertFalse(config.uses_log_plugin({"groups": {"a": "A"}}))

    def test_group_overlap_cache_and_missing_boundaries(self):
        parser = logmarkers.StockPhases(HANDHELD, "chatqna")
        parser.feed("2026-11-24T10:00:00Z", "Downloading model Intel/neural-chat-7b-v3-3 from HuggingFace Hub...")
        steps = parser.finish()
        self.assertEqual(steps[0]["status"], "unavailable")
        self.assertIsNone(steps[0]["seconds"])
        data = build(steps + [step("container_download", "a", 0, 10.0), step("container_download", "b", 5.0, 10.0)])
        group = data["groups"]["container_download"]
        self.assertEqual((group["summed_seconds"], group["active_wall_seconds"]), (20.0, 15.0))
        self.assertIsNone(data["groups"]["model_download"]["summed_seconds"])
        self.assertEqual(data["groups"]["model_download"]["unavailable"], 1)

    def test_service_log_steps_nest_under_the_recorded_step_of_their_service(self):
        parent = step("prep", "Detector preparation", 0, 100.0, service="model-downloader")
        phase = logmarkers.interval("model_download", "Weights", "2026-11-24T10:00:10+00:00",
                                    "2026-11-24T10:00:20+00:00", "stock-log-boundary",
                                    service="model-downloader")
        data = build([parent, phase], total=100.0)
        child = next(s for s in data["steps"] if s["label"] == "Weights")
        self.assertEqual(child["parent_id"], next(s["id"] for s in data["steps"] if s["label"] == "Detector preparation"))
        self.assertAlmostEqual(data["idle_seconds"], 0.0)
        self.assertIn("†", render.markdown(data))

    def test_stock_pip_weights_and_exports_are_separate(self):
        parser = logmarkers.StockPhases(HANDHELD, "model-downloader")
        messages = [
            "Syncing base dependencies with UV", "SUCCESS: All plugin venvs created",
            "Activating virtual environment in /home/appuser/.virtualenvs/dlstreamer...",
            "Downloading and converting: /opt/models/ultralytics/public/yolo26s",
            "\x1b[34mPyTorch:\x1b[0m starting from 'yolo26s.pt'", "Export complete (2.3s)",
            "PyTorch: starting from 'yolo26s.pt'", "Export complete (2.4s)",
            "PyTorch: starting from 'yolo26s.pt'", "Export complete (8.5s)",
        ]
        for index, message in enumerate(messages):
            parser.feed(f"2026-11-24T10:00:{index:02d}Z", message)
        steps = parser.finish()
        self.assertEqual([s["group"] for s in steps],
                         ["model_packages", "model_packages", "model_download",
                          "model_transformation", "model_transformation", "model_transformation"])
        self.assertIn("INT8 (incl. calibration data)", steps[-1]["label"])
        self.assertTrue(all(s["seconds"] == 1 for s in steps))

    def test_chat_exports_and_embedded_downloads_are_not_fabricated(self):
        parser = logmarkers.StockPhases(HANDHELD, "chatqna")
        parser.feed("2026-11-24T10:00:00Z", "Converting a/b model to OpenVINO toolkit format...")
        parser.feed("2026-11-24T10:00:02Z", "Converting c/d model to OpenVINO toolkit format...")
        parser.feed("2026-11-24T10:00:09Z", "Application startup complete.")
        steps = parser.finish()
        self.assertEqual((steps[0]["group"], steps[0]["seconds"]), ("model_transformation", 2))
        self.assertEqual((steps[1]["group"], steps[1]["seconds"]), ("model_combined", 7))
        for service, start, finish in [
            ("audio-analyzer", "utils.ensure_model: Exporting openai/whisper-medium", "utils.ensure_model: Export successful"),
            ("tts", "Exporting SpeechT5 model microsoft/speecht5_tts to OpenVINO IR",
             "Using cached OpenVINO TTS model at models/openvino"),
        ]:
            parser = logmarkers.StockPhases(HANDHELD, service)
            parser.feed("2026-11-24T10:00:00Z", start)
            parser.feed("2026-11-24T10:00:04Z", finish)
            steps = parser.finish()
            self.assertEqual((len(steps), steps[0]["group"], steps[0]["seconds"]), (1, "model_combined", 4))

    def test_raw_logs_and_tokens_are_not_retained(self):
        parser = logmarkers.StockPhases(HANDHELD, "model-downloader")
        parser.feed(*logmarkers.log_entry(
            '2026-11-24T10:00:00.123456789Z {"event":"Starting download: '
            "{'HF_TOKEN': 'sensitive-fixture'}\"}"))
        self.assertEqual(parser.finish(), [])
        self.assertIsNone(logmarkers.log_entry("not a timestamp"))
        self.assertIsNone(logmarkers.log_entry("2026-11-24T10:00:00Z {partial"))

    def test_missing_docker_still_generates_report(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, ASSET_ROOT=temporary), \
                patch.object(cli, "execute", return_value=1), \
                patch.object(logmarkers.subprocess, "run", side_effect=FileNotFoundError("docker")):
            with patch("sys.stdout"):
                self.assertEqual(cli.run(["make"], "demo-up", config_path=str(REPO / "startup-timing.toml")), 1)
            data = json.loads(next((Path(temporary) / "startup-timings").glob("*/report.json")).read_text())
        self.assertIn("Docker inspection unavailable", data["warnings"][0])


class TestImages(unittest.TestCase):
    def test_cached_images_not_pulled_and_backend_not_pulled(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, STARTUP_TIMING_DIR=temporary), \
                patch.object(images.subprocess, "check_output",
                             return_value="intel/chatqna:1\nb:2\nintel/chatqna:1\nhandheld/backend:dev\n"):
            def command(args, **_kwargs):
                code = 1 if args[:3] == ["docker", "image", "inspect"] and args[-1] == "b:2" else 0
                return subprocess.CompletedProcess(args, code, stdout="250000000\n")
            with patch.object(images.subprocess, "run", side_effect=command) as run:
                self.assertEqual(images.pull_images(HANDHELD, ["docker", "compose", "config", "--images"]), 0)
            pulls = [call.args[0] for call in run.call_args_list if call.args[0][1] == "pull"]
            self.assertEqual(pulls, [["docker", "pull", "b:2"]])
            steps = journal.read_steps(Path(temporary) / "events.jsonl")
        self.assertEqual({s["status"] for s in steps}, {"ok", "cached"})
        pulled = next(s for s in steps if s["status"] == "ok")
        self.assertEqual((pulled["group"], pulled["bytes"]), ("container_download", 250000000))
        self.assertEqual(next(s for s in steps if s["status"] == "cached")["service"], "chatqna")
        self.assertIn("b:2 (250 MB)", render.markdown(build([dict(pulled, offset=0.0)])))


class TestReviewFixes(unittest.TestCase):
    def test_parent_keeps_time_its_children_do_not_cover(self):
        from startup_timing import services
        steps = [step("model_preparation", "Prepare detector", 0.0, 100.0, id="p", service="d"),
                 step("model_download", "Download", 10.0, 10.0, parent_id="p", service="d")]
        rows = {a["group"]: a["wall"] for a in services.activities(build(steps, total=100.0), {})}
        self.assertAlmostEqual(rows["model_preparation"], 90.0)
        self.assertAlmostEqual(rows["model_download"], 10.0)

    def test_parent_with_cached_child_stays_the_explanation(self):
        from startup_timing import services
        steps = [step("startup", "Start service", 0.0, 40.0, id="p", service="x"),
                 step("startup", "Cache hit", 0.0, 0.0, "cached", parent_id="p", service="x")]
        row = services.analyse(build(steps, total=40.0), {})[0]
        self.assertEqual([p["label"] for p in row["parts"]], ["Start service"])
        self.assertEqual(services.summary(build(steps, total=40.0), {})["services"][0]["work"][0]["seconds"], 40.0)

    def test_compare_ties_are_ordered_by_step(self):
        first = build([step("b", "Two", 0, 1.0), step("a", "One", 1, 1.0)])
        second = build([step("b", "Two", 0, 1.0), step("a", "One", 1, 1.0)])
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for name, data in (("a.json", first), ("b.json", second)):
                paths.append(Path(temporary, name))
                paths[-1].write_text(json.dumps(data))
            text = compare.compare(*paths)
        self.assertLess(text.index("One"), text.index("Two"))

    def test_report_failure_keeps_the_commands_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(cli, "finalise", side_effect=KeyError("service")), \
                patch.object(cli, "execute", return_value=7):
            self.assertEqual(cli.run(["x"], output=temporary), 7)

    def test_log_plugin_failure_becomes_a_warning(self):
        cfg = {"log_rules": [{}]}
        with patch.object(logmarkers, "observe_services", side_effect=ValueError("bad")):
            steps, warnings = cli.collect_logs(cfg, "a", "b")
        self.assertEqual(steps, [])
        self.assertIn("ValueError", warnings[0])

    def test_unwritable_journal_never_raises(self):
        with patch.dict(os.environ, STARTUP_TIMING_DIR="/nonexistent/dir"):
            journal.end(journal.begin("g", "x"))

    def test_relative_report_root_survives_a_changed_directory(self):
        script = "cd /; source " + str(HELPERS) + "; timed g 'After cd' sleep 0.1"
        with tempfile.TemporaryDirectory() as temporary:
            result = run_tool(script, temporary, "--output", "rel")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("After cd", Path(temporary, "rel", "latest", "report.md").read_text())

    def test_image_pulls_are_capped_and_retried(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, STARTUP_TIMING_DIR=temporary), \
                patch.object(images.subprocess, "check_output", return_value="\n".join(f"i{n}:1" for n in range(9))), \
                patch.object(images, "ThreadPoolExecutor", wraps=images.ThreadPoolExecutor) as pool:
            attempts = []

            def command(args, **_kwargs):
                if args[1] == "pull":
                    attempts.append(args[-1])
                code = 1 if args[1] == "image" or (args[1] == "pull" and attempts.count(args[-1]) == 1) else 0
                return subprocess.CompletedProcess(args, code, stdout="1\n")
            with patch.object(images.subprocess, "run", side_effect=command):
                self.assertEqual(images.pull_images({}, ["x"]), 0)
        self.assertEqual(pool.call_args.kwargs["max_workers"], 4)
        self.assertEqual(len(attempts), 18)  # every first pull failed, every retry worked

    def test_unwritable_report_root_still_runs_the_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary, "file")
            blocker.write_text("x")
            with patch.object(cli, "execute", return_value=5) as execute:
                self.assertEqual(cli.run(["x"], output=str(blocker / "sub")), 5)
            execute.assert_called_once()
            self.assertNotIn("STARTUP_TIMING_DIR", os.environ)

    def test_summary_default_follows_the_report_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = run_tool("source " + str(HELPERS) + "; timed g 'Work' sleep 0.1", temporary,
                              env={"STARTUP_TIMING_ROOT": str(Path(temporary, "elsewhere"))})
            self.assertEqual(result.returncode, 0, result.stderr)
            shown = subprocess.run(["bash", str(HELPERS), "summary", "--json"], cwd=temporary, text=True, capture_output=True,
                                   env={**os.environ, "STARTUP_TIMING_ROOT": str(Path(temporary, "elsewhere"))})
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout)["schema"], "startup-timing-summary/1")

    def test_log_phase_nests_under_the_startup_interval_and_is_the_biggest_cost(self):
        from startup_timing import services
        steps = [step("service_startup", "Chat: start to application ready", 0.0, 100.0, service="chat",
                      method="stock-log"),
                 step("model_download", "Download model", 5.0, 90.0, service="chat", method="stock-log"),
                 step("readiness", "Wait", 95.0, 5.0, service="chat")]
        data = build(steps, total=100.0)
        child = next(s for s in data["steps"] if s["label"] == "Download model")
        self.assertIsNotNone(child["parent_id"])
        row = services.analyse(data, {})[0]
        self.assertEqual(row["parts"][0]["label"], "Download model")
        self.assertEqual(services.summary(data, {})["services"][0]["work"][0]["seconds"], 90.0)

    def test_only_overlapping_pulls_are_a_batch(self):
        from startup_timing import services
        pulls = [step("container_download", f"image-{n}", 1.0, 50.0, service=f"s{n}") for n in range(4)]
        later = step("container_download", "Backend base", 70.0, 10.0, service="backend")
        data = build(pulls + [later], total=100.0)
        names = {r["service"]: r for r in services.analyse(data, {})}
        self.assertAlmostEqual(names["backend"]["busy"], 10.0)
        self.assertIsNone(names["s0"]["busy"])  # batched pulls are counted once, under activities

    def test_compare_does_not_pair_steps_of_different_services(self):
        def run(order):
            return build([step("startup", "Start", 0, seconds, service=name) for name, seconds in order])
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for name, data in (("a.json", run([("a", 1.0), ("b", 9.0)])), ("b.json", run([("b", 9.0), ("a", 1.0)]))):
                paths.append(Path(temporary, name))
                paths[-1].write_text(json.dumps(data))
            text = compare.compare(*paths)
        self.assertNotIn("+8", text)
        self.assertNotIn("-8", text)

    def test_broken_config_warns_and_the_command_still_runs(self):
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "startup-timing.toml").write_text("not = [valid")
            result = run_tool("echo started", temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("started", result.stdout)
            self.assertIn("continuing without configuration", result.stderr)

    def test_shell_script_has_a_bash_shebang(self):
        self.assertEqual(HELPERS.read_text().splitlines()[0], "#!/usr/bin/env bash")


class TestHandheldMake(unittest.TestCase):
    def test_make_entrypoint_and_side_effect_free_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = dict(os.environ)
            env.pop("STARTUP_TIMING_DIR", None)
            result = subprocess.run([
                "make", "--no-print-directory", "demo-up", "INNER_MAKE=true", "CONFIRM=yes",
                f"ASSET_ROOT={root / 'run'}",
            ], cwd=REPO, env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Report:", result.stdout)
            self.assertTrue((root / "run/startup-timings/latest/report.md").exists())
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
                '  printf "make %s\\n" "$*" >> "$TIMING_COMMAND_LOG"\n'
                '  if [[ "$*" == *detector-model* || "$*" == *demo-videos* ]]; then return 0; fi\n'
                '  command make "$@"\n'
                "}\n")
            env = dict(os.environ, BASH_ENV=str(commands),
                       ASSET_ROOT=str(root / "assets"), TIMING_COMMAND_LOG=str(root / "commands"))
            for build_code in (0, 5):
                env["FAKE_BUILD_CODE"] = str(build_code)
                env.pop("STARTUP_TIMING_DIR", None)
                result = subprocess.run([
                    sys.executable, str(TIMING), "run", "--name", "demo-up", "--no-logs", "--",
                    "make", "--no-print-directory", "demo-up-run",
                    *(f"-o{target}" for target in ("preflight", "licenses", "container-images",
                                                   "asset-permissions", "setup")),
                    "RENDER_GID=123", "ACCEL_GID=456",
                ], cwd=REPO, env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Report:", result.stdout)
            reports = [r for r in (root / "assets/startup-timings").glob("*/report.json") if not r.parent.is_symlink()]
            self.assertEqual(len(reports), 2)
            build_statuses = []
            for path in reports:
                report = json.loads(path.read_text())
                self.assertGreater(report["overall_seconds"], 0)
                self.assertTrue(any(step["group"] == "readiness" for step in report["steps"]))
                self.assertTrue((path.parent / "report.md").read_text().startswith("# Startup timing:"))
                build_statuses.extend(step["status"] for step in report["steps"]
                                      if step["group"] == "backend_build")
            self.assertEqual(set(build_statuses), {"ok", "failed"})
            self.assertIn("previously built Backend", result.stderr)
            # Model services start before the detector and demo videos are prepared.
            log = (root / "commands").read_text()
            order = [log.index(text) for text in ("up -d chatqna audio-analyzer tts", "make --no-print-directory detector-model",
                                                  "make --no-print-directory demo-videos", "up -d coturn")]
            self.assertEqual(order, sorted(order))


if __name__ == "__main__":
    unittest.main()
