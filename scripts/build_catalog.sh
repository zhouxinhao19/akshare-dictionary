#!/usr/bin/env bash
# 从字典仓库的全量 JSON 重建 data/catalog.json。用法同 build_catalog.py：
#   bash build_catalog.sh --src <字典仓库>/output/akshare_dictionary.json
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_pyenv.sh
. "$DIR/_pyenv.sh"
PY="$(akdict_pick_python)" || exit 2
exec "$PY" "$DIR/build_catalog.py" "$@"
