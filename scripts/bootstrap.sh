#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null 2>&1 || {
  echo "uv отсутствует. Установите его по docs/SETUP.md и откройте новый терминал." >&2
  exit 1
}
if [[ ! -f "$PROJECT_ROOT/.env" ]]; then
  echo "Создайте .env: cp .env.example .env (из корня проекта)." >&2
  exit 1
fi
cd "$PROJECT_ROOT/backend"
uv sync --locked
uv run alembic upgrade head
printf '%s\n' 'Готово. Из backend: uv run uvicorn app.main:app --reload'   'Проверки: uv run pytest; uv run ruff check .'   'Swagger: http://localhost:8000/docs'
