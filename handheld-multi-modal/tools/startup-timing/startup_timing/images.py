# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Optional plugin: pull a Compose project's images in parallel and time each pull."""
from concurrent.futures import ThreadPoolExecutor
import subprocess

from . import journal


def service_for(cfg, image):
    """Longest configured image prefix wins ([image_services] in the config)."""
    prefixes = cfg.get("image_services", {})
    best = max((p for p in prefixes if image.startswith(p)), key=len, default=None)
    return prefixes.get(best)


def pull_images(cfg, command):
    """Run `command` (prints image names), then pull the missing ones concurrently."""
    section = cfg.get("pull", {})
    group = section.get("group", "container_download")
    images = subprocess.check_output(command, text=True).splitlines()
    images = sorted(set(images) - set(section.get("exclude_images", [])))

    def pull(image):
        service = service_for(cfg, image)
        present = subprocess.run(["docker", "image", "inspect", image],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if present.returncode == 0:
            journal.cached(group, image, service)
            print(f"Container cached: {image}", flush=True)
            return 0
        token = journal.begin(group, image, service)
        code = subprocess.run(["docker", "pull", image]).returncode
        if code:  # one retry: registries and proxies drop connections
            code = subprocess.run(["docker", "pull", image]).returncode
        found = subprocess.run(["docker", "image", "inspect", "-f", "{{.Size}}", image],
                               capture_output=True, text=True).stdout.strip() if code == 0 else ""
        journal.end(token, code, size=int(found) if found.isdigit() else None)
        return code

    # Pulls overlap and share bandwidth; the timeline shows them side by side.
    # [pull] max_parallel (default 4) bounds the load on the registry and the host.
    limit = int(section.get("max_parallel", 4))
    with ThreadPoolExecutor(max_workers=max(1, min(len(images), limit))) as pool:
        codes = list(pool.map(pull, images))
    return next((code for code in codes if code), 0)
