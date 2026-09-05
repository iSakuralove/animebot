"""错误分类。

区分两件事：
  - UserError：用户的问题（参数错、没搜到、没权限）。原文回给用户，日志 info。
  - 其它异常：我们的问题。给用户一句带 trace_id 的道歉，日志 exception。

这个边界不划清楚，最后一定是「要么把栈追踪喷给用户，要么把真 bug 咽掉」。
"""

from __future__ import annotations


class BotError(Exception):
    """本项目所有受控异常的根。"""

    user_message = "出错了，请稍后再试"


class UserError(BotError):
    """用户可修正的错误。message 直接展示给用户。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


class UsageError(UserError):
    """参数用法错。附上用法提示。"""

    def __init__(self, message: str, usage: str | None = None) -> None:
        super().__init__(f"{message}\n\n用法: {usage}" if usage else message)


class PermissionDenied(UserError):
    def __init__(self, message: str = "你没有权限使用这个指令") -> None:
        super().__init__(message)


class RateLimited(UserError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"操作太频繁了，请 {retry_after:.0f} 秒后再试")
        self.retry_after = retry_after


class NotFound(UserError):
    def __init__(self, what: str) -> None:
        super().__init__(f"没找到 {what}")


class ExternalServiceError(BotError):
    """外部 API 挂了 / 超时。是我们的问题，但可重试。/bgm、/agent 会用到。"""

    def __init__(self, service: str, detail: str = "") -> None:
        super().__init__(f"{service} 调用失败: {detail}" if detail else f"{service} 调用失败")
        self.service = service
        self.user_message = f"{service} 暂时用不了，稍后再试"


class ConfigError(BotError):
    """配置缺失或非法。启动期就该炸，不该拖到运行时。"""
