"""Logs, metrics, health. What makes an unattended batch operable.

Nothing here needs a vendor. Structured logs go to stdout as one JSON
object per line, which is what Cloud Logging, Cloud Run, and every
container platform already collect without configuration. Metrics are
Prometheus text format on a plain stdlib HTTP server, so a GKE
ServiceMonitor or a local curl both work and nothing has to be installed.

The severity field is spelled the way Cloud Logging expects, because a
batch job that runs at 3am is worth being able to filter by severity
without writing a parser first.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

_LEVELS = {"debug": "DEBUG", "info": "INFO", "warn": "WARNING", "error": "ERROR"}


def log(event: str, level: str = "info", **fields: Any) -> None:
    """One JSON object per line on stdout. The only logging in this project."""
    record = {
        "severity": _LEVELS.get(level, "INFO"),
        "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "service": "shutter-farm",
    }
    for key, value in fields.items():
        record[key] = value if isinstance(value, (str, int, float, bool, list, dict)) \
            or value is None else str(value)
    print(json.dumps(record, ensure_ascii=True, sort_keys=False), flush=True)


class Metrics:
    """A tiny counters-and-gauges registry. Thread-safe, no dependencies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple], float] = {}
        self._gauges: dict[tuple[str, tuple], float] = {}
        self._help: dict[str, tuple[str, str]] = {}

    def describe(self, name: str, kind: str, help_text: str) -> None:
        self._help[name] = (kind, help_text)

    def inc(self, name: str, value: float = 1.0, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set(self, name: str, value: float, **labels: str) -> None:
        key = (name, tuple(sorted(labels.items())))
        with self._lock:
            self._gauges[key] = value

    def render(self) -> str:
        """Prometheus text exposition format."""
        lines: list[str] = []
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
        seen: set[str] = set()
        for store, default_kind in ((counters, "counter"), (gauges, "gauge")):
            for (name, labels), value in sorted(store.items()):
                if name not in seen:
                    kind, help_text = self._help.get(name, (default_kind, name))
                    lines.append(f"# HELP {name} {help_text}")
                    lines.append(f"# TYPE {name} {kind}")
                    seen.add(name)
                label_str = ""
                if labels:
                    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
                    label_str = "{" + inner + "}"
                rendered = f"{value:.6g}"
                lines.append(f"{name}{label_str} {rendered}")
        return "\n".join(lines) + "\n"


def _escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


METRICS = Metrics()
METRICS.describe("shutter_farm_jobs_total", "counter",
                 "Jobs dispatched, by tool and result")
METRICS.describe("shutter_farm_job_duration_seconds_total", "counter",
                 "Total seconds spent running tools, by tool")
METRICS.describe("shutter_farm_media_files_total", "counter",
                 "Media files covered by completed jobs, by kind")
METRICS.describe("shutter_farm_folders", "gauge",
                 "Folders known to the ledger, by status")
METRICS.describe("shutter_farm_last_run_timestamp_seconds", "gauge",
                 "Unix time of the last completed sweep")
METRICS.describe("shutter_farm_up", "gauge", "1 when the farm process is alive")


class _Handler(BaseHTTPRequestHandler):
    ready_check = staticmethod(lambda: True)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        if self.path.startswith("/metrics"):
            self._send(200, METRICS.render(), "text/plain; version=0.0.4")
        elif self.path.startswith("/healthz"):
            self._send(200, "ok\n", "text/plain")
        elif self.path.startswith("/readyz"):
            ok = bool(self.ready_check())
            self._send(200 if ok else 503, "ready\n" if ok else "not ready\n", "text/plain")
        else:
            self._send(404, "not found\n", "text/plain")

    def _send(self, code: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # noqa: A003 - silence stdlib access logs
        return  # Access logs would drown the structured ones. Metrics cover this.


def serve_metrics(port: int, ready_check=lambda: True) -> HTTPServer:
    """Start the metrics and health server on a daemon thread."""
    handler = type("Handler", (_Handler,), {"ready_check": staticmethod(ready_check)})
    server = HTTPServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name="shutter-farm-metrics")
    thread.start()
    METRICS.set("shutter_farm_up", 1)
    log("metrics_server_started", port=port,
        endpoints=["/metrics", "/healthz", "/readyz"])
    return server


def excepthook_to_log() -> None:
    """Send unhandled exceptions through the structured logger too."""
    def hook(exc_type, exc, tb):
        log("unhandled_exception", level="error",
            error_type=exc_type.__name__, error=str(exc))
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
