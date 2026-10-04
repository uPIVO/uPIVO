$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv отсутствует. Установите его по docs/SETUP.md и откройте новый терминал."
}
if (-not (Test-Path (Join-Path $ProjectRoot ".env"))) {
    throw "Из корня выполните Copy-Item .env.example .env и запустите PostgreSQL."
}
Push-Location (Join-Path $ProjectRoot "backend")
try {
    uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
    uv run alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Migration failed; проверьте доступность PostgreSQL." }
    Write-Host "Готово. Из backend: uv run uvicorn app.main:app --reload"
    Write-Host "Проверки: uv run pytest; uv run ruff check ."
    Write-Host "Swagger: http://localhost:8000/docs"
} finally {
    Pop-Location
}
