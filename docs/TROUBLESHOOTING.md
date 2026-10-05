# Типовые проблемы

Команды Compose выполняются в корне, uv — в backend.
`start.py` выполняется из корня; он сам выбирает директории для этих команд.
На Podman вместо `docker compose` в командах диагностики используйте
`.tools/compose/bin/podman-compose` (Windows: `.tools/compose/Scripts/podman-compose.exe`),
либо установленный `podman-compose`.

| Симптом | Вероятная причина | Решение |
| --- | --- | --- |
| bind: address already in use на 5432 | Уже работает PostgreSQL | В `.env` задайте `POSTGRES_PORT=5433`; локальный DATABASE_URL тоже на 5433; `docker compose up -d --wait postgres` |
| Порт 8000 занят | Другой API | В `.env` APP_PORT=8001 для Docker; локально `uv run uvicorn app.main:app --reload --port 8001`; Swagger на 8001 |
| Порт 6379 занят | Уже запущен Redis | В `.env` задайте `REDIS_PORT=6380` и `REDIS_URL=redis://localhost:6380/0`; `docker compose up -d --wait redis` |
| Cannot connect to Docker daemon | Docker не запущен или нет прав | Запустите Docker Desktop/Engine, проверьте `docker info`; на Linux доступ к socket настройте с администратором |
| env file not found | Нет `.env` | Из корня `cp .env.example .env` или PowerShell `Copy-Item .env.example .env` |
| `/health/db` возвращает 503 | База недоступна, неправильный URL/пароль | `docker compose ps`; `docker compose logs postgres`; проверьте localhost локально и postgres внутри Compose |
| relation does not exist / migration не применена | Схема отстаёт | Из backend `uv run alembic current`, `uv run alembic upgrade head`; Docker: `docker compose run --rm migrate` |
| uv: command not found | uv отсутствует или PATH не обновлён | Установите uv по SETUP, откройте новый терминал, `uv --version` |
| Python version mismatch | Выбран другой Python | Из backend `uv python install 3.12`, `uv sync --locked --python 3.12`, `uv run python --version` |
| Telegram credentials отсутствуют | Ключи не получены | Получите API ID/hash лично или у тимлида безопасно; health/API работают без них |
| password authentication failed после изменения .env | Volume содержит старого пользователя/пароль | POSTGRES_* применяются при первом создании БД; верните исходные dev-credentials или согласуйте изменение пользователя в БД; не удаляйте volume с нужными данными |
| Контейнер API не отражает изменения файлов | Image не пересобран | `docker compose up --build -d --wait` или локальный uvicorn с reload |

Для диагностики: `docker compose logs api migrate`, `docker compose logs postgres`.
Не публикуйте логи с секретами или вывод `.env`. Если меняете локальный DATABASE_URL,
пароль со спецсимволами требует URL-encoding. Наличие `/health` не означает,
что миграции применены или БД доступна: проверяйте `/health/db` и alembic current.

Остановка сохраняет данные: `docker compose down`. Удаление volume необратимо;
оно не является обычным способом исправления ошибок подключения.
