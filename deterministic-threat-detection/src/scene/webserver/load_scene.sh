#!/bin/bash
# SPDX-FileCopyrightText: (C) 2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
#
# Imports the django-archive appdata package into the SceneScape database.
# Runs once in the scene-loader container; skipped if the scene already exists.
set -euo pipefail

APPDIR=/home/scenescape/Scenescape
WORK=$(mktemp -d)
cd "${APPDIR}"

# manager.secrets is normally provided by the webserver entrypoint.
[ -f manager/secrets.py ] || cp /run/secrets/django/secrets.py manager/secrets.py

tar -xjf /opt/appdata.tar.bz2 -C "${WORK}"
SCENE_NAME=$(python3 -c "
import json,sys
d=json.load(open('${WORK}/data.json'))
print(next(o['fields']['name'] for o in d if o['model']=='manager.scene'))")

if python3 manage.py shell -c "
from manager.models import Scene
import sys
sys.exit(0 if Scene.objects.filter(name='${SCENE_NAME}').exists() else 1)"; then
  echo "Scene ${SCENE_NAME} already present, nothing to do."
  exit 0
fi

# Files referenced by the fixture (maps, thumbnails) live next to data.json.
find "${WORK}" -type f ! -name data.json ! -name meta.json \
  -exec cp -n {} /workspace/media/ \;

# Fixture rows that the stack already created (default assets, db status).
python3 - "${WORK}/data.json" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
skip = {"manager.databasestatus"}
json.dump([o for o in d if o["model"] not in skip], open(p, "w"))
EOF

python3 manage.py loaddata "${WORK}/data.json"
echo "Imported scene ${SCENE_NAME}."
