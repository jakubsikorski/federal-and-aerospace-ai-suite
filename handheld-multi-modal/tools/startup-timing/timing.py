#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Entry point: python3 timing.py run|begin|end|render|compare|pull ..."""
from startup_timing.cli import entry

if __name__ == "__main__":
    raise SystemExit(entry())
