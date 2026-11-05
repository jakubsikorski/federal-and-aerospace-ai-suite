#!/usr/bin/env bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Download the detection model into src/data/models/omz/ (skipped when already present and valid).
#   ./download_models.sh
#
# person-detection-retail-0013 (FP32) from Open Model Zoo, Apache-2.0. The URL is pinned to a release
# and every file is checked against a SHA-256, so a changed or truncated download is rejected.
#
# The first download asks you to accept the model's license. Without a yes the script exits with an
# error (so `make up` stops). The answer is remembered in models/.terms-accepted and not asked again.
# Non-interactive use: ACCEPT_MODEL_TERMS=yes ./download_models.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
NAME=person-detection-retail-0013
BASE=${MODEL_BASE_URL:-https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1}
DEST=$HERE/models/omz/$NAME/FP32

declare -A SHA256=(
  [$NAME.xml]=33acc96a73898256a127a53af71b6bfb169df83bd514984b01beacbcf7b2cc04
  [$NAME.bin]=f3731a76de6a26ef7f380a08831bc13d74fc1e6ae774fd9ea30267a487084f2c
)

valid() { [ -f "$1" ] && [ "$(sha256sum "$1" | cut -d' ' -f1)" = "$2" ]; }

LICENSE_URL=https://raw.githubusercontent.com/openvinotoolkit/open_model_zoo/master/LICENSE
ACCEPTED=$HERE/models/.terms-accepted

need_download=0
for f in "${!SHA256[@]}"; do valid "$DEST/$f" "${SHA256[$f]}" || need_download=1; done

accept_terms() {
  [ -f "$ACCEPTED" ] && return 0
  echo
  echo "The detection model \"$NAME\" is not part of this repository. It is downloaded from the"
  echo "Open Model Zoo (Intel, https://github.com/openvinotoolkit/open_model_zoo) and is provided under"
  echo "its own license, not under this project's:"
  echo
  echo "    $LICENSE_URL   (Apache License 2.0)"
  echo
  echo "By continuing you confirm that you have read the license and accept its terms for this model."
  echo
  local answer=${ACCEPT_MODEL_TERMS:-}
  if [ -z "$answer" ]; then
    if [ ! -t 0 ]; then
      echo "error: the model license must be accepted. Run this in a terminal, or set ACCEPT_MODEL_TERMS=yes" >&2
      return 1
    fi
    read -r -p "Do you accept the license and download the model? [y/N] " answer
  fi
  case "$answer" in
    y|Y|yes|YES|Yes) mkdir -p "$(dirname "$ACCEPTED")"; date -u +%FT%TZ > "$ACCEPTED" ;;
    *) echo "License not accepted: the model is not downloaded and the setup is aborted." >&2; return 1 ;;
  esac
}

[ "$need_download" = 0 ] || accept_terms || exit 1

mkdir -p "$DEST"
for f in "${!SHA256[@]}"; do
  if valid "$DEST/$f" "${SHA256[$f]}"; then
    continue
  fi
  echo "downloading $f"
  curl -fsSL --retry 3 --connect-timeout 15 -o "$DEST/$f.part" "$BASE/$NAME/FP32/$f" \
    || { rm -f "$DEST/$f.part"; echo "error: cannot download $BASE/$NAME/FP32/$f" >&2; exit 1; }
  if ! valid "$DEST/$f.part" "${SHA256[$f]}"; then
    rm -f "$DEST/$f.part"
    echo "error: $f does not match the expected SHA-256" >&2
    exit 1
  fi
  mv "$DEST/$f.part" "$DEST/$f"
done
echo "models ready: $DEST"
