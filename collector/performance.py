#!/usr/bin/env python3
"""Request-scoped report performance diagnostics and memoisation."""

from __future__ import annotations

import contextvars
import hashlib
import json
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


_CURRENT = contextvars.ContextVar("powermonitor_performance_context", default=None)
_LATEST: Optional[dict[str, Any]] = None


@dataclass
class PerformanceContext:
    selected_period: dict[str, Any]
    started_at: float = field(default_factory=time.perf_counter)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    timings: OrderedDict[str, float] = field(default_factory=OrderedDict)
    cache_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    _cache: dict[str, Any] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str):
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings[name] = self.timings.get(name, 0.0) + (time.perf_counter() - start) * 1000

    def increment(self, name: str, amount: int = 1) -> None:
        self.counters[name] = int(self.counters.get(name, 0)) + int(amount)

    def memoize(self, namespace: str, key: Any, loader: Callable[[], Any]) -> Any:
        safe_key = _safe_key(namespace, key)
        stats = self.cache_stats.setdefault(namespace, {"hits": 0, "misses": 0})
        if safe_key in self._cache:
            stats["hits"] += 1
            return self._cache[safe_key]
        stats["misses"] += 1
        value = loader()
        self._cache[safe_key] = value
        return value

    def as_dict(self) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.started_at) * 1000
        slowest = sorted(
            ({"stage": name, "milliseconds": round(value, 3)} for name, value in self.timings.items()),
            key=lambda item: item["milliseconds"],
            reverse=True,
        )[:10]
        return {
            "generated_at": self.generated_at,
            "selected_period": dict(self.selected_period),
            "total_generation_ms": round(total_ms, 3),
            "stage_timings_ms": {name: round(value, 3) for name, value in self.timings.items()},
            "slowest_stages": slowest,
            "cache": {name: dict(values) for name, values in self.cache_stats.items()},
            "counters": dict(self.counters),
        }


@contextmanager
def performance_context(selected_period: Optional[dict[str, Any]] = None):
    global _LATEST
    context = PerformanceContext(selected_period=safe_period(selected_period or {}))
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _LATEST = context.as_dict()
        _CURRENT.reset(token)


def current_context() -> Optional[PerformanceContext]:
    return _CURRENT.get()


@contextmanager
def stage(name: str):
    context = current_context()
    if context is None:
        yield
        return
    with context.stage(name):
        yield


def memoize(namespace: str, key: Any, loader: Callable[[], Any]) -> Any:
    context = current_context()
    if context is None:
        return loader()
    return context.memoize(namespace, key, loader)


def increment(name: str, amount: int = 1) -> None:
    context = current_context()
    if context is not None:
        context.increment(name, amount)


def timer_start() -> float:
    return time.perf_counter()


def record_timing(name: str, started_at: float) -> None:
    context = current_context()
    if context is not None:
        context.timings[name] = context.timings.get(name, 0.0) + (time.perf_counter() - started_at) * 1000


def latest_diagnostics() -> dict[str, Any]:
    return dict(_LATEST or {})


def settings_fingerprint(settings: Any) -> str:
    raw = getattr(settings, "__dict__", settings)
    return _digest(raw)


def file_fingerprint(path: Any) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
    except OSError:
        return {"missing": True}


def safe_period(period: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(period.get("kind") or ""),
        "label": str(period.get("label") or ""),
        "start": str(period.get("start") or ""),
        "end": str(period.get("end") or ""),
    }


def _safe_key(namespace: str, key: Any) -> str:
    return f"{namespace}:{_digest(key)}"


def _digest(value: Any) -> str:
    payload = json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BoundedHtmlCache:
    def __init__(self, max_entries: int = 8) -> None:
        self.max_entries = max_entries
        self._values: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        if key not in self._values:
            return None
        value = self._values.pop(key)
        self._values[key] = value
        return value

    def set(self, key: str, value: str) -> None:
        if key in self._values:
            self._values.pop(key)
        self._values[key] = value
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    def clear(self) -> None:
        self._values.clear()

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._values), "max_entries": self.max_entries}
