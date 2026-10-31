#!/bin/sh
# npm wrapper supporting either system Node.js or this project's local tools.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
    node_dir="$project_dir/.tools/node-v24.22.2-darwin-arm64/bin"
    if [ ! -x "$node_dir/node" ]; then
        echo '请安装受支持的 Node.js（>=22.12），然后重新执行此命令。' >&2
        exit 1
    fi
    PATH="$node_dir:$PATH"
    export PATH
fi
exec npm "$@"
