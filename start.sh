#!/usr/bin/env bash
# course2md 启动脚本（Linux / macOS）
# 用法：./start.sh [--port 8765] [--cpu] [--check] [--no-open]
set -euo pipefail

cd "$(dirname "$0")"

PORT="${COURSE2MD_PORT:-8765}"
HOST="${COURSE2MD_HOST:-127.0.0.1}"
OPEN="yes"
CHECK_ONLY="no"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --host) HOST="$2"; shift 2 ;;
        --cpu) export COURSE2MD_DEVICE=cpu COURSE2MD_COMPUTE_TYPE=int8; shift ;;
        --check) CHECK_ONLY="yes"; shift ;;
        --no-open) OPEN="no"; shift ;;
        -h|--help)
            sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 2 ;;
    esac
done

# 优先使用项目内的 .venv；不存在时用 uv 创建（只在首次创建，之后不会自动同步）
if [[ -n "${COURSE2MD_PYTHON:-}" ]]; then
    PY="$COURSE2MD_PYTHON"
elif [[ -x .venv/bin/python ]]; then
    PY=.venv/bin/python
elif command -v uv >/dev/null 2>&1; then
    echo "==> 首次运行：uv sync --extra asr"
    uv sync --extra asr
    PY=.venv/bin/python
else
    PY=python3
    echo "!   未安装 uv，使用系统 Python；请确认已执行 pip install -e '.[asr]'" >&2
fi
command -v "$PY" >/dev/null 2>&1 || { echo "找不到 $PY，请安装 uv 或 Python 3.10+" >&2; exit 1; }

echo "==> 环境自检"
"$PY" -m course2md doctor
if [[ "$CHECK_ONLY" == "yes" ]]; then exit 0; fi

echo "==> 启动界面 http://$HOST:$PORT"
args=(-m course2md web --host "$HOST" --port "$PORT")
[[ "$OPEN" == "no" ]] && args+=(--no-open)
exec "$PY" "${args[@]}"
