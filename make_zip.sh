#!/usr/bin/env bash
# Собирает mystery-shopper.zip для deploy.sh без venv, скриншотов, логов, баз и env-файлов.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -f mystery-shopper.zip
zip -qr mystery-shopper.zip mystery-shopper \
  -x 'mystery-shopper/*/.venv/*' 'mystery-shopper/*/screenshots/*' 'mystery-shopper/formbot/run*/*' \
     'mystery-shopper/*.db' 'mystery-shopper/*/*.db' 'mystery-shopper/*/*.log' 'mystery-shopper/*/*.csv.bak' \
     'mystery-shopper/*/formbot_log.csv' 'mystery-shopper/*/diag_sites.json' 'mystery-shopper/*/*.orig' \
     'mystery-shopper/*/.env' 'mystery-shopper/.env' 'mystery-shopper/*/__pycache__/*' 'mystery-shopper/*/report.xlsx' 'mystery-shopper/voximplant/credentials.json' 'mystery-shopper/*/*_private.json' 'mystery-shopper/local.env' 'mystery-shopper/logs/*' 'mystery-shopper/telegram/state.json'
echo "собран $(pwd)/mystery-shopper.zip"; unzip -l mystery-shopper.zip | tail -1
