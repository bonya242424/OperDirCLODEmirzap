#!/usr/bin/env bash
# Обновляет систему на сервере: забирает код, пересобирает и перезапускает контейнеры,
# ждёт, пока сайт начнёт отвечать. При сбое откатывает код на предыдущую версию.
set -euo pipefail
cd "$(dirname "$0")/.."

PREV=$(git rev-parse HEAD)
git fetch --quiet origin main
git reset --hard origin/main
NEW=$(git rev-parse HEAD)
echo "Версия: ${PREV:0:7} -> ${NEW:0:7}"

wait_ready() {
  for _ in $(seq 1 30); do
    if docker compose exec -T backend python -c \
      "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=3).status==200 else 1)" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

docker compose up -d --build
if wait_ready; then
  echo "Готово: система отвечает."
  docker image prune -f >/dev/null
  # Только обезличенный статус проверки входов и очередей; секреты не читаются этим отчётом.
  docker compose exec -T backend python -m app.supplier_credentials --wait-status || echo "Статус автовхода пока недоступен; сервис работает."
else
  echo "Система не поднялась, откатываю на ${PREV:0:7}" >&2
  git reset --hard "$PREV"
  docker compose up -d --build
  exit 1
fi
