"""
In-process fakes that mimic a real deployment target, so example plugins read
like real infrastructure code without needing a cluster, a database, or a
network. Deterministic by construction (no wall-clock, no randomness) so tests
are stable.

``FakeCluster``  — apply/wait-ready/delete a ``FakeDeployment`` (think kubectl).
``FakeDeployment`` — wraps a ``FakeService`` and a readiness countdown.
``FakeService``  — an HTTP-ish service with a seedable store, deterministic
                   latency, and a ``degrade()`` switch to simulate a fault's
                   downstream effect (errors + latency blow-up).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict


class RequestRecord(TypedDict):
    """One logged request. A plain dict at runtime, so it serializes into the
    record's JSON diagnostics; typed so ``status`` comparisons stay clean."""

    path: str
    status: int
    latency_ms: float


class FakeResponse:
    """A minimal HTTP-ish response."""

    def __init__(self, status: int, latency_ms: float, body: object = None) -> None:
        self.status = status
        self.latency_ms = latency_ms
        self.body = body

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class FakeService:
    """A deterministic web service backing a deployment.

    ``request`` returns 200 with a cycling latency while healthy. Once
    ``degrade()`` is called, a configurable fraction of requests return 503 and
    latency multiplies — modelling the downstream effect of an injected fault.
    """

    healthy_latencies_ms: tuple[float, ...] = (40.0, 45.0, 38.0, 50.0, 42.0)
    degraded_latency_multiplier: float = 6.0
    degraded_error_every: int = 2  # every Nth request 503s while degraded
    store: dict[str, str] = field(default_factory=dict)
    request_log: list[RequestRecord] = field(default_factory=list)
    _degraded: bool = False
    _n: int = 0

    def degrade(self) -> None:
        self._degraded = True

    def recover(self) -> None:
        self._degraded = False

    def request(self, path: str = "/") -> FakeResponse:
        i = self._n
        self._n += 1
        base = self.healthy_latencies_ms[i % len(self.healthy_latencies_ms)]
        if self._degraded:
            is_error = (i % self.degraded_error_every) == 0
            status = 503 if is_error else 200
            latency = base * self.degraded_latency_multiplier
        else:
            status, latency = 200, base
        resp = FakeResponse(status=status, latency_ms=latency)
        self.request_log.append(
            RequestRecord(path=path, status=status, latency_ms=latency)
        )
        return resp

    # Seed / data helpers — stand in for POST /records, INSERT, etc.
    def put(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def drop(self, key: str) -> None:
        self.store.pop(key, None)


class FakeDeployment:
    """A deployment that becomes ready after ``ready_after`` readiness polls."""

    def __init__(self, name: str, *, image: str, replicas: int, ready_after: int) -> None:
        self.name = name
        self.image = image
        self.replicas = replicas
        self.service = FakeService()
        self._ready_polls_remaining = ready_after
        self.deleted = False

    def poll_ready(self) -> bool:
        if self._ready_polls_remaining > 0:
            self._ready_polls_remaining -= 1
        return self._ready_polls_remaining == 0 and not self.deleted


class DeploymentNotReady(Exception):
    pass


class FakeCluster:
    """A tiny stand-in for a cluster API (apply / wait-ready / delete / list)."""

    def __init__(self, *, ready_after: int = 2) -> None:
        self.ready_after = ready_after
        self.deployments: dict[str, FakeDeployment] = {}

    async def apply(self, name: str, *, image: str, replicas: int = 1) -> FakeDeployment:
        dep = FakeDeployment(
            name, image=image, replicas=replicas, ready_after=self.ready_after
        )
        self.deployments[name] = dep
        return dep

    async def wait_ready(self, name: str, *, max_polls: int = 10) -> None:
        dep = self.deployments[name]
        for _ in range(max_polls):
            if dep.poll_ready():
                return
        raise DeploymentNotReady(f"{name} not ready after {max_polls} polls")

    async def delete(self, name: str) -> bool:
        dep = self.deployments.pop(name, None)
        if dep is None:
            return False
        dep.deleted = True
        return True

    def names(self) -> list[str]:
        return sorted(self.deployments)
