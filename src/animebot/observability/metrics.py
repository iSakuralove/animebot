"""埋点。进程内聚合，`/metrics` 指令直接读，不依赖外部 Prometheus。

刻意做得很小：计数器 + 直方图两种，够回答"哪个指令在被用、哪个在报错、
哪个慢"。真需要 Prometheus 时，从 snapshot() 导出就行，业务埋点代码不用改。
"""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# 延迟分桶（毫秒）。分界点照着真实体感取：100ms 内无感，1s 开始有感，5s 是能忍的上限。
_BUCKETS = (50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0)


@dataclass(slots=True)
class Histogram:
    count: int = 0
    total: float = 0.0
    min: float = float("inf")
    max: float = 0.0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(_BUCKETS) + 1))

    def observe(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.min = min(self.min, value)
        self.max = max(self.max, value)
        for i, edge in enumerate(_BUCKETS):
            if value <= edge:
                self.buckets[i] += 1
                return
        self.buckets[-1] += 1

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0

    def quantile(self, q: float) -> float:
        """桶内线性近似。不追求精确，只用来发现"变慢了"。"""
        if not self.count:
            return 0.0
        target = self.count * q
        seen = 0
        for i, n in enumerate(self.buckets):
            seen += n
            if seen >= target:
                return _BUCKETS[i] if i < len(_BUCKETS) else self.max
        return self.max

    def as_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "avg_ms": round(self.avg, 1),
            "p50_ms": round(self.quantile(0.5), 1),
            "p95_ms": round(self.quantile(0.95), 1),
            "max_ms": round(self.max, 1),
        }


class Metrics:
    """线程安全（aiogram 单线程，但 CLI/测试可能多线程，锁的成本可以忽略）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter[str] = Counter()
        self._hists: dict[str, Histogram] = defaultdict(Histogram)
        self._started = time.time()

    def incr(self, name: str, value: int = 1, **labels: Any) -> None:
        with self._lock:
            self._counters[_key(name, labels)] += value

    def observe(self, name: str, ms: float, **labels: Any) -> None:
        with self._lock:
            self._hists[_key(name, labels)].observe(ms)

    @contextmanager
    def timer(self, name: str, **labels: Any) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - t0) * 1000, **labels)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "uptime_s": int(time.time() - self._started),
                "counters": dict(sorted(self._counters.items())),
                "timings": {k: h.as_dict() for k, h in sorted(self._hists.items())},
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._hists.clear()
            self._started = time.time()


def _key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    tail = ",".join(f"{k}={v}" for k, v in sorted(labels.items()) if v is not None)
    return f"{name}{{{tail}}}" if tail else name


METRICS = Metrics()
