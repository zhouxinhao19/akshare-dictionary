#!/usr/bin/env bash
# 检索 AKShare 接口目录。用法与参数同 lookup.py，例如：
#   bash lookup.sh 日线 A股
#   bash lookup.sh --category futures 库存
#   bash lookup.sh --name stock_zh_a_hist
# 自动挑选可用的 Python，可在任意 cwd 下用绝对路径调用。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_pyenv.sh
. "$DIR/_pyenv.sh"
PY="$(akdict_pick_python)" || exit 2
exec "$PY" "$DIR/lookup.py" "$@"
