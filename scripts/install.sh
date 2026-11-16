#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11+"'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
mkdir -p data logs reports
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
fi
.venv/bin/python main.py validate
echo '安装完成。编辑 markets.json 配置市场；编辑 .env 配置运行模式。运行 bash scripts/start.sh。'
