#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Show the third-party software a start downloads and ask the user to accept it.

The list comes from third-party-components.spdx, so it stays in step with the
SBOM. CONFIRM=yes accepts without asking (for scripted starts).
"""
import argparse
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPDX = REPO / "third-party-components.spdx"
# Pulled only for the demo cameras (make demo-prereq / demo-up).
DEMO_ONLY = {"ffmpeg"}
# File name -> license of the clips scripts/demo_assets.py downloads.
DEMO_VIDEO_LICENSES = {
    "classroom.avi": "Apache-2.0",
    "person-bicycle-car-detection.mp4": "CC-BY-4.0",
    "pexels-34622113.mp4": "Pexels License",
}


def read_spdx(path):
    """Return [(section title, [package dict])], skipping the application itself."""
    text = re.sub(r"<text>(.*?)</text>", lambda m: " ".join(m.group(1).split()),
                  path.read_text(encoding="utf-8"), flags=re.S)
    sections, current, lines = [], None, text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# ") and i and lines[i - 1].startswith("####"):
            current = (line[2:].strip(), [])
            sections.append(current)
            continue
        m = re.match(r"^(Package[A-Za-z]+):\s*(.*)$", line)
        if not m or current is None:
            continue
        key, value = m.groups()
        if key == "PackageName":
            current[1].append({})
        if current[1]:
            current[1][-1].setdefault(key, value)
    return [(title, pkgs) for title, pkgs in sections[1:] if pkgs]


def demo_videos():
    sys.path.insert(0, str(REPO / "scripts"))
    from demo_assets import VIDEOS  # pylint: disable=import-outside-toplevel
    return [{"PackageName": name, "PackageLicenseConcluded": DEMO_VIDEO_LICENSES.get(name, "see source"),
             "PackageDownloadLocation": url} for name, url, _ in VIDEOS]


def cells(pkg):
    version = pkg.get("PackageVersion", "")
    if re.fullmatch(r"[0-9a-f]{40}", version):  # Hugging Face revision
        version = version[:12]
    return pkg["PackageName"], version, pkg.get("PackageLicenseConcluded", "NOASSERTION")


def render(sections):
    rows = [cells(p) for _, pkgs in sections for p in pkgs]
    widths = [max(len(r[i]) for r in rows) for i in range(3)]
    out = []
    for title, pkgs in sections:
        out.append(f"\n{title}")
        for p in pkgs:
            out.append("  " + "  ".join(c.ljust(w) for c, w in zip(cells(p), widths))
                       + "  " + p.get("PackageDownloadLocation", ""))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="include the demo camera image and videos")
    args = parser.parse_args()

    sections = []
    for title, pkgs in read_spdx(SPDX):
        pkgs = [p for p in pkgs if args.demo or p["PackageName"] not in DEMO_ONLY]
        if pkgs:
            sections.append((title, pkgs))
    if args.demo:
        sections.append(("Demo camera videos (scripts/demo_assets.py)", demo_videos()))

    print("This start downloads the following third-party software and models to this device,\n"
          "if they are not already present. Each is provided under its own license.")
    print(render(sections))
    print("\nTheir own dependencies (transitive Python packages, operating-system packages in the\n"
          "images) are not listed. Details: third-party-components.spdx\n")

    if os.environ.get("CONFIRM") == "yes":
        print("Licenses accepted with CONFIRM=yes.")
        return 0
    if not sys.stdin.isatty():
        print("Cannot ask for confirmation without a terminal. Review the licenses above and\n"
              "re-run with CONFIRM=yes to accept them.", file=sys.stderr)
        return 1
    print("Accept these licenses and continue? [y/N] ", end="", flush=True)
    answer = sys.stdin.readline().strip().lower()
    if answer in ("y", "yes"):
        return 0
    print("Not accepted; nothing was downloaded. Re-run with CONFIRM=yes to accept without asking.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
