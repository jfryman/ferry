"""Prometheus metrics for `ferry serve`, in the text exposition format, with no dependencies.

Every label value comes from a small fixed set (job actions, results, reject reasons), never from a
request: no usernames, branch names, error text, URLs or credentials end up in /metrics, and the
series count stays bounded whatever webhooks arrive.
"""

from __future__ import annotations

import threading
from typing import Callable

ACTIONS = ("plan", "apply", "unlock", "drift")
RESULTS = ("ok", "failed", "refused", "skipped", "error")
REJECT_REASONS = ("bad_auth", "duplicate", "stale", "future", "undated", "bad_json", "foreign_repo",
                  "no_access", "head_moved", "queue_full", "too_large", "bad_request", "disabled")
DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 3600)

HELP = {
    "ferry_jobs_total": ("counter", "Jobs finished, by action and result (ok, failed, refused, skipped, error)."),
    "ferry_job_duration_seconds": ("histogram", "How long finished jobs took, by action."),
    "ferry_jobs_queued_total": ("counter", "Jobs queued from webhooks and the drift schedule, by action."),
    "ferry_webhook_rejected_total": ("counter", "Webhook deliveries refused, by reason."),
    "ferry_last_job_timestamp_seconds": ("gauge", "When the last job of each action finished."),
    "ferry_queue_depth": ("gauge", "Jobs waiting for the worker."),
    "ferry_locks_held": ("gauge", "Inventory locks currently held."),
    "ferry_build_info": ("gauge", "The running ferry version."),
    "ferry_enabled": ("gauge", "1 if plan, apply or automatic plans are enabled on this ferry, 0 if switched off."),
}


def reject_reason(status: int, text: str) -> str | None:
    """The fixed reason label for a webhook answer, or None if it wasn't a rejection."""
    t = text or ""
    rules = (
        (status == 401, "bad_auth"),
        (t in ("duplicate delivery", "duplicate comment"), "duplicate"),
        (t.startswith("stale event"), "stale"),
        (t == "event timestamp is in the future", "future"),
        (t == "event has no readable timestamp", "undated"),
        (t == "bad json", "bad_json"),
        (t.startswith("not this service's"), "foreign_repo"),
        ("refused: no " in t, "no_access"),
        (t.endswith("the head isn't the one commented on"), "head_moved"),
        (t.endswith("refused: disabled") or t == "autoplan disabled", "disabled"),
        (status == 503, "queue_full"),
        (status == 413, "too_large"),
        (status in (400, 411), "bad_request"),
    )
    return next((reason for hit, reason in rules if hit), None)


def _labels(labels: dict) -> str:
    if not labels:
        return ""
    esc = {k: str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"') for k, v in labels.items()}
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(esc.items())) + "}"


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._gauges: dict[tuple[str, tuple], float] = {}
        self._hist: dict[tuple, list] = {}  # (name, labels) -> [bucket counts..., sum, count]

    @staticmethod
    def _key(name: str, labels: dict) -> tuple[str, tuple]:
        return name, tuple(sorted(labels.items()))

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        with self._lock:
            k = self._key(name, labels)
            self._counters[k] = self._counters.get(k, 0.0) + value

    def set(self, name: str, value: float, **labels) -> None:
        with self._lock:
            self._gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, **labels) -> None:
        with self._lock:
            h = self._hist.setdefault(self._key(name, labels), [0] * len(DURATION_BUCKETS) + [0.0, 0])
            for i, le in enumerate(DURATION_BUCKETS):
                if value <= le:
                    h[i] += 1
            h[-2] += value
            h[-1] += 1

    def job_done(self, action: str, result: str, seconds: float, finished: float) -> None:
        self.inc("ferry_jobs_total", action=action, result=result)
        self.observe("ferry_job_duration_seconds", seconds, action=action)
        self.set("ferry_last_job_timestamp_seconds", finished, action=action)

    def render(self, live: dict[str, Callable[[], float]] | None = None, version: str = "") -> str:
        """The exposition text; `live` gauges are read now (queue depth, locks held)."""
        with self._lock:
            counters, gauges = dict(self._counters), dict(self._gauges)
            hist = {k: list(v) for k, v in self._hist.items()}
        for name, read in (live or {}).items():
            try:
                gauges[(name, ())] = float(read())
            except Exception:  # a broken reader drops its line, never the endpoint
                pass
        if version:
            gauges[("ferry_build_info", (("version", version),))] = 1.0
        # Zero-valued series for the fixed label sets, so rates and alerts work from the start.
        for a in ACTIONS:
            for r in RESULTS:
                counters.setdefault(("ferry_jobs_total", (("action", a), ("result", r))), 0.0)
        for reason in REJECT_REASONS:
            counters.setdefault(("ferry_webhook_rejected_total", (("reason", reason),)), 0.0)
        out = []
        for name, (kind, text) in HELP.items():
            lines = []
            if kind == "histogram":
                for (n, labels), h in sorted(hist.items()):
                    if n != name:
                        continue
                    base = dict(labels)
                    for i, le in enumerate(DURATION_BUCKETS):
                        lines.append(f"{name}_bucket{_labels({**base, 'le': le})} {h[i]}")
                    lines.append(f"{name}_bucket{_labels({**base, 'le': '+Inf'})} {h[-1]}")
                    lines.append(f"{name}_sum{_labels(base)} {_num(h[-2])}")
                    lines.append(f"{name}_count{_labels(base)} {h[-1]}")
            else:
                src = counters if kind == "counter" else gauges
                for (n, labels), v in sorted(src.items()):
                    if n == name:
                        lines.append(f"{name}{_labels(dict(labels))} {_num(v)}")
            if lines:
                out += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", *lines]
        return "\n".join(out) + "\n"
