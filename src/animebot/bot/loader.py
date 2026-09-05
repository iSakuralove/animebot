"""模块加载。按名字动态 import，app 层不认识任何具体模块名。"""

from __future__ import annotations

import importlib

from ..config import Settings
from ..core.container import Container
from ..core.errors import ConfigError
from ..core.feature import BaseFeature
from ..observability.logging import get_logger

log = get_logger("animebot.loader")

_PKG = "animebot.features"


def load_feature(name: str, settings: Settings, container: Container) -> BaseFeature:
    """约定优于配置：animebot.features.<name> 里必须有 FEATURE 指向 Feature 子类。"""
    try:
        mod = importlib.import_module(f"{_PKG}.{name}")
    except ModuleNotFoundError as exc:
        raise ConfigError(f"功能模块 {name!r} 不存在（找不到 {_PKG}.{name}）") from exc

    factory = getattr(mod, "FEATURE", None)
    if factory is None:
        raise ConfigError(f"模块 {_PKG}.{name} 必须导出 FEATURE")

    feature = factory(settings, container)
    if not feature.name:
        feature.name = name
    if feature.requires:
        container.require(*feature.requires)
    return feature


def load_features(settings: Settings, container: Container) -> list[BaseFeature]:
    out: list[BaseFeature] = []
    seen: set[str] = set()
    for name in settings.features:
        if name in seen:
            continue
        seen.add(name)
        feature = load_feature(name, settings, container)
        out.append(feature)
        log.info("feature.loaded", feature=feature.name)
    return out
