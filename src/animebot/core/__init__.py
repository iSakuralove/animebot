"""内核：注册表、容器、模块协议、错误类型。不依赖 aiogram。"""

from .container import Container
from .errors import (
    BotError,
    ConfigError,
    ExternalServiceError,
    NotFound,
    PermissionDenied,
    RateLimited,
    UsageError,
    UserError,
)
from .feature import BaseFeature, Feature
from .registry import CommandRegistry, CommandSpec, RateLimit, command, spec_of

__all__ = [
    "BaseFeature",
    "BotError",
    "CommandRegistry",
    "CommandSpec",
    "ConfigError",
    "Container",
    "ExternalServiceError",
    "Feature",
    "NotFound",
    "PermissionDenied",
    "RateLimit",
    "RateLimited",
    "UsageError",
    "UserError",
    "command",
    "spec_of",
]
