"""Тестовая блокировка общего прохода: без PostgreSQL, с наблюдаемым состоянием.

Unit-тесты на SQLite не могут взять advisory lock PostgreSQL, поэтому явно
подставляют эту реализацию. Боевой код не имеет «безопасного» отключения
блокировки: без PostgreSQL он падает с понятной ошибкой.
"""

from __future__ import annotations


class FakeRunLock:
    """Реализация ``RunLock`` для тестов.

    ``busy`` — блокировку уже держит другой процесс, ``lost_after`` — потеря
    блокировки после указанного числа успешных проверок.
    """

    def __init__(self, *, busy: bool = False, lost_after: int | None = None) -> None:
        self.busy = busy
        self.lost_after = lost_after
        self.acquired = 0
        self.released = 0
        self.verified = 0
        self.live = True

    def acquire(self) -> bool:
        self.acquired += 1
        return not self.busy

    def verify(self) -> bool:
        self.verified += 1
        if not self.live:
            raise RuntimeError("соединение с PostgreSQL разорвано")
        if self.lost_after is not None and self.verified > self.lost_after:
            return False
        return True

    def release(self) -> None:
        self.released += 1
