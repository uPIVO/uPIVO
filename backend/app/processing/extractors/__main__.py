"""Поддержка обратной совместимости для запуска пакета как CLI."""

from app.processing.extractors.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
