# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""End-to-end test of the deterministic-threat-detection stack through the
SceneScape UI and data flow. Run with `make test`.

Checks: UI login, hota-scene listed with both cameras, scene page renders,
pipelines RUNNING, camera MQTT messages carry the SEI `frame`, and the scene
controller publishes tracks seen by both cameras, and the regulated topic
used by the UI carries objects.
"""
import json
import pathlib
import subprocess
import sys

import requests
import urllib3
from playwright.sync_api import sync_playwright

urllib3.disable_warnings()
ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://localhost"
SCENE = "hota-scene"
CAMERAS = ["Cam_x1_0", "Cam_x2_0"]
PASSWORD = (ROOT / "secrets/supass").read_text().strip()
OUT = ROOT / "tests" / "artifacts"
OUT.mkdir(exist_ok=True, parents=True)
results = []


def check(name, ok, detail=""):
  results.append(ok)
  print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")


def mqtt_once(topic, wait=15):
  cmd = ["docker", "compose", "exec", "-T", "broker", "mosquitto_sub", "-h", "localhost", "-p", "1883",
         "--cafile", "/mosquitto/secrets/root-cert", "--insecure", "-t", topic, "-C", "1", "-W", str(wait)]
  out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True).stdout.strip()
  return json.loads(out) if out else None


def api_headers():
  token = requests.post(f"{BASE}/api/v1/auth", data={"username": "admin", "password": PASSWORD},
                        verify=False).json()["token"]
  return {"Authorization": f"Token {token}"}


def scene_uid():
  scenes = requests.get(f"{BASE}/api/v1/scenes", headers=api_headers(), verify=False).json()["results"]
  return next((s["uid"] for s in scenes if s["name"] == SCENE), None)


def ui_tests():
  with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_context(ignore_https_errors=True, viewport={"width": 1600, "height": 1000}).new_page()
    page.goto(BASE + "/")
    page.fill("input[name=username]", "admin")
    page.fill("input[name=password]", PASSWORD)
    page.click("button[type=submit], input[type=submit]")
    page.wait_for_load_state("networkidle")
    check("UI login", "login" not in page.url, page.url)
    page.screenshot(path=OUT / "1-scenes.png")
    check("UI lists scene", SCENE in page.content())

    page.goto(f"{BASE}/{scene_uid()}/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(8000)
    page.screenshot(path=OUT / "2-scene.png")
    body = page.content()
    check("UI scene page shows cameras", all(c in body for c in CAMERAS))
    page.click("text=Show Telemetry")
    page.click("text=Show Trails")
    try:
      page.wait_for_function("!/Rate:\\s*--/.test(document.body.innerText)", timeout=20000)
    except Exception:
      pass
    rate = page.inner_text("text=/Rate:/").strip()
    check("UI scene receives live data (rate)", "--" not in rate, rate)
    marks = page.evaluate("document.querySelectorAll('svg circle, svg image, svg g.object, svg rect').length")
    check("UI plots tracked objects", marks > 0, f"{marks} svg marks")
    page.screenshot(path=OUT / "2-scene.png")

    page.goto(BASE + "/cam/list/")
    page.wait_for_load_state("networkidle")
    page.screenshot(path=OUT / "3-cameras.png")
    check("UI camera list", all(c in page.content() for c in CAMERAS))
    browser.close()


def flow_tests():
  h = api_headers()
  uid = scene_uid()
  check("API scene present", uid is not None, str(uid))
  cams = requests.get(f"{BASE}/api/v1/cameras", headers=h, verify=False).json()["results"]
  check("API cameras present", sorted(c["name"] for c in cams) == CAMERAS)

  st = requests.get(f"{BASE}/api/pipelines/status", verify=False).json()
  check("2 pipelines RUNNING", len(st) == 2 and all(s["state"] == "RUNNING" for s in st),
        str([round(s["avg_fps"], 1) for s in st]))

  for cam in CAMERAS:
    m = mqtt_once(f"scenescape/data/camera/{cam}")
    check(f"{cam} detections carry SEI frame", bool(m) and "frame" in m and "person" in m.get("objects", {}),
          f"frame={m and m.get('frame')}")

  m = mqtt_once(f"scenescape/data/scene/{uid}/person")
  fused = [o for o in (m or {}).get("objects", []) if set(CAMERAS) <= set(o.get("visibility", []))]
  check("scene tracks fused from both cameras", len(fused) > 0, f"{len(fused)} fused")

  m = mqtt_once(f"scenescape/regulated/scene/{uid}")
  check("regulated scene topic (feeds the UI)", bool(m) and len(m.get("objects", [])) > 0)


if __name__ == "__main__":
  ui_tests()
  flow_tests()
  print(f"{sum(results)}/{len(results)} checks passed; screenshots in {OUT}")
  sys.exit(0 if all(results) else 1)
