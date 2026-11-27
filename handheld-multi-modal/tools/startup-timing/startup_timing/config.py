# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Optional TOML configuration. Everything here is polish; no file means defaults."""
import os
from pathlib import Path
import tomllib

DEFAULT_NAME = "startup-timing.toml"


def find(path=None):
    """The config file in use: path, $STARTUP_TIMING_CONFIG or ./startup-timing.toml, else None."""
    path = path or os.environ.get("STARTUP_TIMING_CONFIG") or (
        DEFAULT_NAME if Path(DEFAULT_NAME).is_file() else None)
    return str(Path(path).resolve()) if path else None


def load(path=None):
    """Return the config dict, or {} when there is no config (the zero-config default)."""
    path = find(path)
    if not path:
        return {}
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Cannot read timing configuration {path}: {error}") from error


def pretty(name):
    """Capitalise the first letter of each lower-case word; leave acronyms like API alone."""
    words = name.replace("_", " ").replace("-", " ").split()
    return " ".join(w[0].upper() + w[1:] if w.islower() else w for w in words)


def group_title(cfg, group):
    return cfg.get("groups", {}).get(group) or pretty(group)


def service_title(cfg, service):
    return cfg.get("services", {}).get(service, {}).get("title") or pretty(service)


def report_root(cfg, override=None):
    """Directory holding one sub-directory per run."""
    if override:
        return Path(override)
    section = cfg.get("report", {})
    if os.environ.get("STARTUP_TIMING_ROOT"):
        return Path(os.environ["STARTUP_TIMING_ROOT"])
    base = os.environ.get(section.get("root_env", ""), "") or section.get("root_default", ".")
    return Path(base) / section.get("directory", "startup-timings")


def uses_log_plugin(cfg):
    return bool(cfg.get("log_rules") or any(
        item.get("container") for item in cfg.get("services", {}).values()))
