"""Thin adapters for the seven stock services.

Each adapter does exactly two things in Phase 1: report reachability, and make
one real call that proves the service is actually usable rather than merely
listening. Endpoints and ports were probed live against the pinned containers on
2026-09-14; none of them are guessed.

Nothing here retries. A timed-out Chat Q&A generation in particular must never
be retried: the stock service exposes no cancellation interface, so a retry
stacks a second generation on a busy GPU.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import httpx

log = logging.getLogger(__name__)


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str
    blocking: bool
    latency_ms: int | None = None
    data: dict | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "blocking": self.blocking,
            "latency_ms": self.latency_ms,
            "data": self.data,
        }


def _probe(name: str, blocking: bool, call: Callable[[], tuple[str, dict | None]]) -> Probe:
    import time

    started = time.monotonic()
    try:
        detail, data = call()
        ok = True
    except Exception as exc:  # a dependency being down is a reported state, not a crash
        detail, data, ok = f"{type(exc).__name__}: {exc}"[:200], None, False
        log.warning("probe %s failed: %s", name, detail)
    return Probe(name, ok, detail, blocking, int((time.monotonic() - started) * 1000), data)


class Services:
    def __init__(self, urls: dict[str, str], timeout: float = 5.0):
        self.urls = urls
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def _get(self, service: str, path: str) -> Any:
        response = self.client.get(self.urls[service] + path)
        response.raise_for_status()
        return response

    # --- one real call per service ------------------------------------------

    def chatqna(self) -> tuple[str, dict | None]:
        """GET /model returns the loaded LLM identity, so it proves the model, not the port."""
        body = self._get("chatqna", "/model").json()
        model = body.get("llm_model")
        expected = "Intel/neural-chat-7b-v3-3"
        if model != expected:
            raise RuntimeError(f"wrong model loaded: {model!r}, expected {expected!r}")
        return f"model {model}", body

    def audio_analyzer(self) -> tuple[str, dict | None]:
        body = self._get("audio_analyzer", "/health").json()
        if body.get("status") != "ok":
            raise RuntimeError(f"health status {body.get('status')!r}")
        return "health ok", body

    def tts(self) -> tuple[str, dict | None]:
        """/v1/model-info proves both provenance-approved model and CPU placement (D52)."""
        body = self._get("tts", "/v1/model-info").json()
        if body.get("device") != "CPU":
            raise RuntimeError(f"TTS device is {body.get('device')!r}, expected CPU")
        return f"{body.get('model')} on {body.get('device')}", {
            "model": body.get("model"),
            "device": body.get("device"),
            "implementation": body.get("implementation"),
            "supported_speakers": body.get("supported_speakers"),
        }

    def dlsps(self) -> tuple[str, dict | None]:
        """GET /pipelines lists loaded definitions; instances are Phase 2."""
        body = self._get("dlsps", "/pipelines").json()
        names = [f"{p.get('name')}/{p.get('version')}" for p in body] if isinstance(body, list) else []
        if not names:
            raise RuntimeError("no pipeline definitions loaded")
        return f"{len(names)} definition(s): {', '.join(names)}", {"definitions": names}

    def mediamtx(self) -> tuple[str, dict | None]:
        body = self._get("mediamtx", "/v3/paths/list").json()
        paths = {item["name"]: bool(item.get("ready")) for item in body.get("items", [])}
        return f"{len(paths)} path(s) configured", {"paths": paths}

    def alert_agent(self) -> tuple[str, dict | None]:
        body = self._get("alert_agent", "/api/v1/health").json()
        return f"health {body.get('status', 'unknown')}", body

    def metrics(self) -> tuple[str, dict | None]:
        """Ask for real samples: an empty result means the collectors are not working.

        Must be /api/v1/metrics/latest. On this pinned 2026.1.0 image the query
        endpoints /api/v1/metrics and /api/v1/metrics/names return
        {"metrics":[],"count":0} unconditionally - with no filter, with a limit
        and with a valid ?name= - while /latest returns 24 live series. Probed
        2026-09-14. Using the query endpoint would report a healthy collector as
        broken.
        """
        body = self.metrics_latest()
        series = body.get("metrics", {})
        if not series:
            raise RuntimeError("no live metric series")
        names = sorted({v.get("name") for v in series.values()})
        return f"{len(series)} live series, {len(names)} distinct", {
            "series_count": len(series),
            "names": names,
        }

    def metrics_latest(self) -> dict:
        return self._get("metrics", "/api/v1/metrics/latest").json()

    # --- checklist -----------------------------------------------------------

    def probe_all(self) -> list[Probe]:
        """Blocking services make the app unusable; degraded ones reduce it.

        Probes run concurrently: sequentially, seven wedged services would make
        /api/health take seven timeouts, and the UI polls it every 5 s, so the
        polls would pile up exactly when something is already wrong. httpx.Client
        is thread-safe, and the order of the returned list is preserved.
        """
        checks: list[tuple[str, bool, Callable]] = [
            ("mediamtx", True, self.mediamtx),
            ("dlsps", True, self.dlsps),
            ("chatqna", True, self.chatqna),
            ("audio_analyzer", False, self.audio_analyzer),
            ("tts", False, self.tts),
            ("alert_agent", False, self.alert_agent),
            ("metrics", False, self.metrics),
        ]
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            return list(pool.map(lambda c: _probe(*c), checks))
