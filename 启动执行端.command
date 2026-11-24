#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if [[ -x "runtime/python/bin/python3.12" ]]; then
    exec "runtime/python/bin/python3.12" -B install_runtime.py "$@"
fi
if [[ -x ".venv/bin/python" ]]; then
    exec ".venv/bin/python" -m film_worker launch "$@"
fi
if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' '请先安装 uv，再重新打开执行端。'
    exit 1
fi
exec uv run --locked python -m film_worker launch "$@"
