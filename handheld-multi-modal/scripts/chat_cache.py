#!/usr/bin/env python3
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""Chat Q&A model cache check. Standalone: the Makefile runs it inside the Chat Q&A image
(PyYAML, standard library only; the host import needs no PyYAML), so it must not import anything else from this repository."""
from pathlib import Path
import time


def repair_chat_cache(directory, models):
    """Stock conversion skips existing directories, even interrupted exports."""
    files = ("openvino_model.xml", "openvino_model.bin", "openvino_tokenizer.xml",
             "openvino_tokenizer.bin", "config.json", "tokenizer_config.json")
    for model in models:
        path = directory / model
        if path.is_dir() and not all((path / name).is_file() and (path / name).stat().st_size for name in files):
            backup = path.with_name(path.name + ".incomplete-" + str(time.time_ns()))
            path.rename(backup)
            print("Preserved incomplete export:", backup)


def chat_cache_ready(directory, models):
    """True when every model has its OpenVINO export and a resolvable Hub snapshot."""
    for model in models:
        if not (directory / model / "openvino_model.xml").is_file():
            return False
        repo = directory / ("models--" + model.replace("/", "--"))
        ref = repo / "refs/main"
        if not ref.is_file() or not (repo / "snapshots" / ref.read_text().strip()).is_dir():
            return False
    return True


def main():
    import yaml  # pylint: disable=import-outside-toplevel  # present in the Chat Q&A image only
    cache = Path("/tmp/model_cache")
    models = yaml.safe_load(Path("/tmp/model_config/config.yaml").read_text())["model_settings"]
    ids = [models[role + "_MODEL_ID"] for role in ("LLM", "EMBEDDING", "RERANKER")]
    repair_chat_cache(cache, ids)
    print("cached" if chat_cache_ready(cache, ids) else "download")


if __name__ == "__main__":
    main()
