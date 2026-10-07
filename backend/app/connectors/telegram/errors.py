class TelegramConnectorError(RuntimeError):
    """Safe base error for local Telegram connector failures."""


class TelegramConfigurationError(TelegramConnectorError):
    """Required local Telegram configuration is missing or invalid."""


class TelegramAuthorizationError(TelegramConnectorError):
    """The configured StringSession is not authorized."""


class TelegramChannelAccessError(TelegramConnectorError):
    """The account cannot resolve or read the requested channel."""
