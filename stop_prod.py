"""Остановка демостенда без удаления данных. Инструкция: docs/LOCAL_DEMO.md."""

import argparse
import sys

import start_prod


def main(argv=None):
    parser = argparse.ArgumentParser(description="Остановка демостенда uPIVO без удаления данных.")
    parser.parse_args(argv)
    try:
        env_file = start_prod._require_env_file()
        # Та же блокировка, что у запуска: одновременные up и down не пересекаются.
        with start_prod._single_run_lock():
            result = start_prod._run(
                [
                    *start_prod._compose(start_prod.MAIN_PROJECT, start_prod.MAIN_COMPOSE, env_file),
                    "--profile",
                    "workers",
                    "down",
                ],
                timeout=300,
            )
    except start_prod.LauncherError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr, flush=True)
        return 1
    except OSError:
        print(
            "Ошибка: не удалось запустить docker: проверьте установку Docker и Docker Compose.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if result.returncode != 0:
        print(
            f"Ошибка: не удалось остановить {start_prod.MAIN_PROJECT}: проверьте Docker Compose и повторите.",
            file=sys.stderr,
            flush=True,
        )
        return 1
    print("Демостенд upivo-demo остановлен. Данные, общий шлюз и ngrok сохранены.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
