#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Handheld entry point for the shared startup timing tool."""
from importlib import import_module
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve()
os.environ.setdefault("STARTUP_TIMING_CONFIG", str(HERE.parents[1] / "startup-timing.toml"))
SHARED = HERE.parents[2] / "tools/startup-timing"
if not SHARED.is_dir():
    SHARED = Path("/tools/startup-timing")
sys.path.insert(0, str(SHARED))
TIMING = import_module("startup_timing_core")

if __name__ == "__main__":
    raise SystemExit(TIMING.main())
sys.modules[__name__] = TIMING
