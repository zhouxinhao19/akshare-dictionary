# 共享的 Python 解析逻辑，被 lookup.sh / check.sh / build_catalog.sh source。
# 用法：PY="$(akdict_pick_python [必需模块])" ；找不到可用解释器时返回非 0 并在 stderr 说明。
#
# 探测顺序：
#   1. 环境变量 AKSHARE_DICT_PYTHON（迁移到新机器时的推荐指定方式）
#   2. PATH 上的 python3 / python —— Windows 商店的 python 占位符能通过
#      "存在性"检查但跑不出结果，这里的 -c 试运行会把它过滤掉
#   3. 常见 conda/venv 安装位置（Git Bash 视角 /c /d ...，WSL 视角 /mnt/...）
#
# 可选参数是"必须能 import 的模块名"（如 check.sh 传 akshare），
# 不满足的解释器会被跳过，避免选中没有依赖的环境。

_akdict_works() { # $1=解释器  $2=要求能 import 的模块（可空）
  if [ -n "$2" ]; then
    "$1" -c "import sys; import $2" >/dev/null 2>&1
  else
    "$1" -c "import sys" >/dev/null 2>&1
  fi
}

akdict_pick_python() {
  local req="${1:-}"
  if [ -n "${AKSHARE_DICT_PYTHON:-}" ]; then
    if _akdict_works "$AKSHARE_DICT_PYTHON" "$req"; then
      printf '%s\n' "$AKSHARE_DICT_PYTHON"
      return 0
    fi
    echo "AKSHARE_DICT_PYTHON=$AKSHARE_DICT_PYTHON 不可用（无法执行或缺少 $req），继续探测其他位置" >&2
  fi

  local candidates=()
  candidates+=(python3 python)
  shopt -s nullglob
  # 注意：Git Bash(MSYS) 下路径开头的字符类 glob（如 /[cde]/）匹配不到，
  # 盘符必须逐一显式写出。
  local drive
  for drive in /c /d /e /f; do
    candidates+=(
      "$drive"/anaconda3/envs/*/python.exe
      "$drive"/miniconda3/envs/*/python.exe
      "$drive"/miniforge3/envs/*/python.exe
      "$drive"/anaconda3/python.exe
      "$drive"/miniconda3/python.exe
      "$drive"/miniforge3/python.exe
    )
  done
  candidates+=(
    "$HOME"/anaconda3/envs/*/python.exe
    "$HOME"/miniconda3/envs/*/python.exe
    "$HOME"/miniforge3/envs/*/python.exe
    "$HOME"/.conda/envs/*/python.exe
    "$HOME"/anaconda3/python.exe
    "$HOME"/miniconda3/python.exe
    "$HOME"/miniforge3/python.exe
    /mnt/*/anaconda3/envs/*/python.exe
    /mnt/*/miniconda3/envs/*/python.exe
  )
  shopt -u nullglob

  local c
  for c in "${candidates[@]}"; do
    if _akdict_works "$c" "$req"; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  if [ -n "$req" ]; then
    echo "未找到可用且带有 $req 的 Python；请设置 AKSHARE_DICT_PYTHON 指向相应环境的 python 可执行文件" >&2
  else
    echo "未找到可用 Python；请设置 AKSHARE_DICT_PYTHON 指向 python 可执行文件" >&2
  fi
  return 1
}
