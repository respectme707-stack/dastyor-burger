#!/usr/bin/env bash
# Обновление сервера до последней версии одной командой.
# С компьютера (PowerShell):
#   ssh -i КЛЮЧ ubuntu@IP "bash ~/dastyor-burger/update.sh"
set -euo pipefail
cd "$(dirname "$0")"
echo "== Скачиваю обновления"
git pull --ff-only
bash install.sh
