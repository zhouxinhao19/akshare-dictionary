#!/usr/bin/env bash
# 现场复测 catalog 里接口的当前可用性。用法同 check.py，例如：
#   bash check.sh stock_zh_a_hist stock_zh_a_daily
#   bash check.sh --timeout 15 fund_etf_hist_em
# 需要 Python 环境里装有 akshare；探测时会自动跳过没有 akshare 的解释器。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=_pyenv.sh
. "$DIR/_pyenv.sh"
PY="$(akdict_pick_python akshare)" || exit 2
exec "$PY" "$DIR/check.py" "$@"
