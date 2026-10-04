#!/usr/bin/env bash
# 每日管线入口（cron 调用）。部署到 ECS 后由 crontab 在 18:40 触发。
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# LLM / git 凭据等敏感项放在同目录的 env.sh（chmod 600），不要提交
if [ -f env.sh ]; then
  # shellcheck disable=SC1091
  . ./env.sh
fi

LOG_DIR="$DIR/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date +%Y%m%d).log"

{
  echo "===== run start $(date '+%F %T %Z') ====="
  python3 pipeline.py
  echo "===== run end   $(date '+%F %T %Z') exit=$? ====="
} >>"$LOG" 2>&1

# 只保留最近 60 天日志
find "$LOG_DIR" -name '*.log' -mtime +60 -delete 2>/dev/null
