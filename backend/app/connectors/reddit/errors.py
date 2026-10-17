class RedditError(Exception):
    """Ошибка получения публикаций из Arctic Shift."""


class RedditRequestError(RedditError):
    """Ошибка HTTP-запроса к Arctic Shift."""


class RedditSaturatedWindowError(RedditError):
    """Диапазон нельзя безопасно получить без пропуска публикаций."""


class RedditSyncConflictError(RedditError):
    """Курсор синхронизации изменился во время получения данных."""
