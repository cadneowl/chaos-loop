"""Tests for StaticHypothesizer + each individual detector."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.diagnostician.tools.code_reader import TargetCodeReader
from agents.tester.detectors import (
    HardcodedSecretDetector,
    HardPodAffinityDetector,
    Issue,
    MissingCircuitBreakerDetector,
    MissingRetryDetector,
    MissingTimeoutDetector,
    NoFallbackForCacheDetector,
    SingleReplicaDetector,
    SyncCallInAsyncDetector,
    default_detectors,
    hypothesis_id,
    slug,
)
from agents.tester.hypothesizer import StaticHypothesizer

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_repo(tmp_path: Path, files: dict[str, str]) -> TargetCodeReader:
    """Materialize a fake target repo and hand back a sandboxed reader."""
    for rel, content in files.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    return TargetCodeReader(tmp_path)


# --------------------------------------------------------------------------- #
# id helpers                                                                  #
# --------------------------------------------------------------------------- #


def test_slug_is_deterministic_and_short() -> None:
    assert slug("services/cart/handler.py:42") == slug("services/cart/handler.py:42")
    assert len(slug("any text", 8)) == 8


def test_hypothesis_id_matches_contract_regex() -> None:
    import re

    issue = Issue(file="services/cart/handler.py", line=42, snippet="x")
    hid = hypothesis_id("missing-timeout", issue)
    assert re.fullmatch(r"^h-[0-9a-z\-]{1,64}$", hid), hid
    assert hid.startswith("h-missing-timeout-")


def test_default_detector_set_has_expected_members() -> None:
    names = {d.name for d in default_detectors()}
    assert {
        "missing-timeout",
        "missing-retry",
        "missing-circuit-breaker",
        "no-fallback-for-cache",
        "sync-call-in-async",
        "single-replica",
        "hard-pod-affinity",
        "hardcoded-secret",
    } <= names


# --------------------------------------------------------------------------- #
# MissingTimeoutDetector                                                      #
# --------------------------------------------------------------------------- #


def test_missing_timeout_flags_requests_call_without_timeout(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/api.py": "import requests\n\ndef fetch():\n    return requests.get('https://x')\n",
    })
    issues = MissingTimeoutDetector().find(code)
    assert len(issues) == 1
    assert issues[0].file.replace("\\", "/") == "src/api.py"
    assert issues[0].line == 4


def test_missing_timeout_skips_call_with_timeout(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/api.py": "import requests\n\ndef fetch():\n    return requests.get('https://x', timeout=5)\n",
    })
    assert MissingTimeoutDetector().find(code) == []


def test_missing_timeout_flags_subprocess_without_timeout(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/run.py": "import subprocess\n\nsubprocess.run(['ls'])\n",
    })
    issues = MissingTimeoutDetector().find(code)
    assert len(issues) == 1


def test_missing_timeout_one_finding_per_line(tmp_path: Path) -> None:
    """Even when both http and subprocess patterns could overlap, we report once."""
    code = _make_repo(tmp_path, {
        "src/x.py": "subprocess.run(['ls'])\nrequests.get('a')\n",
    })
    issues = MissingTimeoutDetector().find(code)
    assert len(issues) == 2  # one per line, not per pattern


# --------------------------------------------------------------------------- #
# MissingRetryDetector                                                        #
# --------------------------------------------------------------------------- #


def test_missing_retry_flags_redis_call_in_file_with_no_retry(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import redis\n\n"
            "def get_cart(user):\n"
            "    return redis.Redis().get(f'cart:{user}')\n"
        ),
    })
    issues = MissingRetryDetector().find(code)
    assert len(issues) == 1
    assert "redis" in issues[0].detail


def test_missing_retry_skips_file_that_uses_tenacity(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import redis\n"
            "from tenacity import retry\n\n"
            "@retry\n"
            "def get_cart(user):\n"
            "    return redis.Redis().get(f'cart:{user}')\n"
        ),
    })
    assert MissingRetryDetector().find(code) == []


def test_missing_retry_skips_file_that_mentions_backoff(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import httpx\n# uses exponential backoff\n"
            "def fetch(): return httpx.get('x')\n"
        ),
    })
    assert MissingRetryDetector().find(code) == []


def test_missing_retry_ignores_import_lines(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": "from redis import Redis\n# No call below\n",
    })
    assert MissingRetryDetector().find(code) == []


def test_missing_retry_one_finding_per_file(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import httpx\n"
            "def a(): return httpx.get('x')\n"
            "def b(): return httpx.post('y')\n"
        ),
    })
    issues = MissingRetryDetector().find(code)
    assert len(issues) == 1


# --------------------------------------------------------------------------- #
# MissingCircuitBreakerDetector                                               #
# --------------------------------------------------------------------------- #


def test_circuit_breaker_flags_external_call_with_no_breaker(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import httpx\n\n"
            "def call(): return httpx.get('https://api')\n"
        ),
    })
    issues = MissingCircuitBreakerDetector().find(code)
    assert len(issues) == 1
    assert issues[0].line == 3


def test_circuit_breaker_skips_file_that_uses_pybreaker(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import httpx\nimport pybreaker\n"
            "breaker = pybreaker.CircuitBreaker()\n"
            "def call(): return httpx.get('x')\n"
        ),
    })
    assert MissingCircuitBreakerDetector().find(code) == []


def test_circuit_breaker_skips_file_with_circuit_decorator(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import httpx\nfrom circuitbreaker import circuit\n"
            "@circuit\ndef call(): return httpx.get('x')\n"
        ),
    })
    assert MissingCircuitBreakerDetector().find(code) == []


def test_circuit_breaker_one_finding_per_file(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import httpx\n"
            "def a(): return httpx.get('x')\n"
            "def b(): return httpx.post('y')\n"
        ),
    })
    assert len(MissingCircuitBreakerDetector().find(code)) == 1


def test_circuit_breaker_ignores_import_and_comment_lines(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "from httpx import AsyncClient\n"
            "# httpx.get is normally fine\n"
        ),
    })
    assert MissingCircuitBreakerDetector().find(code) == []


# --------------------------------------------------------------------------- #
# NoFallbackForCacheDetector                                                  #
# --------------------------------------------------------------------------- #


def test_no_fallback_flags_redis_get_without_try(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/profile.py": (
            "import redis\n"
            "r = redis.Redis()\n"
            "def get(uid): return r.get(f'u:{uid}')\n"
        ),
    })
    issues = NoFallbackForCacheDetector().find(code)
    assert len(issues) == 1
    assert ".get(" in issues[0].detail


def test_no_fallback_skips_file_with_try_block(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/profile.py": (
            "import redis\n"
            "r = redis.Redis()\n"
            "def get(uid):\n"
            "    try:\n"
            "        return r.get(f'u:{uid}')\n"
            "    except Exception:\n"
            "        return None\n"
        ),
    })
    assert NoFallbackForCacheDetector().find(code) == []


def test_no_fallback_flags_memcache_call(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import memcache\n"
            "mc = memcache.Client(['127.0.0.1:11211'])\n"
            "def get(k): return mc.get(k)\n"
        ),
    })
    issues = NoFallbackForCacheDetector().find(code)
    assert len(issues) == 1


def test_no_fallback_flags_valkey_call(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cart.py": (
            "import valkey\n"
            "c = valkey.Valkey()\n"
            "def get(k): return c.get(k)\n"
        ),
    })
    issues = NoFallbackForCacheDetector().find(code)
    assert len(issues) == 1


def test_no_fallback_ignores_non_cache_get(tmp_path: Path) -> None:
    """``requests.get`` is a network call, not a cache GET — don't fire."""
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import requests\n"
            "def call(): return requests.get('https://api')\n"
        ),
    })
    assert NoFallbackForCacheDetector().find(code) == []


# --------------------------------------------------------------------------- #
# SyncCallInAsyncDetector                                                     #
# --------------------------------------------------------------------------- #


def test_sync_in_async_flags_time_sleep_in_async_def(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import time\n\n"
            "async def handler():\n"
            "    time.sleep(1)\n"
            "    return 'ok'\n"
        ),
    })
    issues = SyncCallInAsyncDetector().find(code)
    assert len(issues) == 1
    assert issues[0].line == 4
    assert "time.sleep" in issues[0].detail


def test_sync_in_async_flags_requests_in_async_def(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import requests\n\n"
            "async def fetch():\n"
            "    return requests.get('https://api')\n"
        ),
    })
    issues = SyncCallInAsyncDetector().find(code)
    assert len(issues) == 1


def test_sync_in_async_skips_calls_in_sync_def(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import time\n\n"
            "def handler():\n"
            "    time.sleep(1)\n"
        ),
    })
    assert SyncCallInAsyncDetector().find(code) == []


def test_sync_in_async_skips_call_offloaded_to_thread(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import asyncio\nimport requests\n\n"
            "async def fetch():\n"
            "    return await asyncio.to_thread(requests.get, 'https://api')\n"
        ),
    })
    assert SyncCallInAsyncDetector().find(code) == []


def test_sync_in_async_skips_run_in_executor(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import asyncio\nimport time\n\n"
            "async def wait():\n"
            "    loop = asyncio.get_running_loop()\n"
            "    await loop.run_in_executor(None, time.sleep, 1)\n"
        ),
    })
    assert SyncCallInAsyncDetector().find(code) == []


def test_sync_in_async_scope_ends_at_next_def(tmp_path: Path) -> None:
    """A sync call in a sibling sync def AFTER an async def shouldn't fire."""
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import time\n\n"
            "async def a():\n"
            "    return 1\n"
            "\n"
            "def b():\n"
            "    time.sleep(1)\n"
        ),
    })
    assert SyncCallInAsyncDetector().find(code) == []


def test_sync_in_async_flags_call_in_nested_block(tmp_path: Path) -> None:
    """Deeper indent than async def → still inside its body."""
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import time\n\n"
            "async def handler(n):\n"
            "    for _ in range(n):\n"
            "        time.sleep(0.1)\n"
        ),
    })
    issues = SyncCallInAsyncDetector().find(code)
    assert len(issues) == 1
    assert issues[0].line == 5


def test_sync_in_async_skips_comment_line(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/svc.py": (
            "import time\n\n"
            "async def handler():\n"
            "    # time.sleep(1) — left for reference\n"
            "    return 'ok'\n"
        ),
    })
    assert SyncCallInAsyncDetector().find(code) == []


# --------------------------------------------------------------------------- #
# SingleReplicaDetector                                                       #
# --------------------------------------------------------------------------- #


def test_single_replica_flags_replicas_one_in_deployment(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "k8s/cart.yaml": (
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "metadata:\n  name: cart\n"
            "spec:\n  replicas: 1\n"
        ),
    })
    issues = SingleReplicaDetector().find(code)
    assert len(issues) == 1


def test_single_replica_ignores_replicas_one_in_non_deployment(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "k8s/policy.yaml": "kind: PolicyXyz\nspec:\n  replicas: 1\n",
    })
    assert SingleReplicaDetector().find(code) == []


def test_single_replica_ignores_replicas_two(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "k8s/cart.yaml": "kind: Deployment\nspec:\n  replicas: 2\n",
    })
    assert SingleReplicaDetector().find(code) == []


def test_single_replica_handles_yml_extension(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "deploy/cart.yml": "kind: Deployment\nspec:\n  replicas: 1\n",
    })
    assert len(SingleReplicaDetector().find(code)) == 1


# --------------------------------------------------------------------------- #
# HardPodAffinityDetector                                                     #
# --------------------------------------------------------------------------- #


def test_hard_pod_affinity_flags_required_block(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "k8s/cart.yaml": (
            "kind: Deployment\nspec:\n  template:\n    spec:\n"
            "      affinity:\n        podAffinity:\n"
            "          requiredDuringSchedulingIgnoredDuringExecution:\n"
            "            - labelSelector: {}\n"
        ),
    })
    issues = HardPodAffinityDetector().find(code)
    assert len(issues) == 1


def test_hard_pod_affinity_skips_preferred_only(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "k8s/cart.yaml": (
            "spec:\n  affinity:\n    podAffinity:\n"
            "      preferredDuringSchedulingIgnoredDuringExecution:\n"
            "        - weight: 100\n"
        ),
    })
    assert HardPodAffinityDetector().find(code) == []


# --------------------------------------------------------------------------- #
# HardcodedSecretDetector                                                     #
# --------------------------------------------------------------------------- #


def test_hardcoded_secret_flags_obvious_hardcoded_key(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cfg.py": 'API_KEY = "sk_live_abcdefghijklmnop"\n',
    })
    issues = HardcodedSecretDetector().find(code)
    assert len(issues) == 1


def test_hardcoded_secret_skips_env_loaded_assignment(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/cfg.py": 'API_KEY = os.environ.get("API_KEY", "fallback_default")\n',
    })
    assert HardcodedSecretDetector().find(code) == []


def test_hardcoded_secret_skips_test_directory(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "tests/test_auth.py": 'PASSWORD = "test_password_for_fixture"\n',
    })
    assert HardcodedSecretDetector().find(code) == []


def test_hardcoded_secret_skips_comment_lines(tmp_path: Path) -> None:
    """Example secrets in code COMMENTS shouldn't fire — that's where docs and
    examples live, not actual leakage. Fixes the false positive we hit in our
    own detectors/secrets.py docstring/comments."""
    code = _make_repo(tmp_path, {
        "src/cfg.py": (
            "# Example: API_KEY = \"sk_live_abcdefghijklmnop\"\n"
            "# password = 'hunter2hunter2hunter2'\n"
            "# (these are documentation, not real secrets)\n"
        ),
    })
    assert HardcodedSecretDetector().find(code) == []


def test_hardcoded_secret_skips_short_literal(tmp_path: Path) -> None:
    """Variable named 'token' assigned a short literal isn't suspicious enough."""
    code = _make_repo(tmp_path, {
        "src/cfg.py": 'token = "x"\n',
    })
    assert HardcodedSecretDetector().find(code) == []


# --------------------------------------------------------------------------- #
# StaticHypothesizer aggregation                                              #
# --------------------------------------------------------------------------- #


def test_static_hypothesizer_aggregates_across_detectors(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/api.py": "import requests\n\ndef f(): return requests.get('x')\n",
        "k8s/d.yaml": "kind: Deployment\nspec:\n  replicas: 1\n",
    })
    h = StaticHypothesizer()
    hyps = asyncio.run(
        h.generate(target_app="x", target_repo=None, code=code)
    )
    assert len(hyps) >= 2
    fault_names = {hyp.proposed_fault for hyp in hyps}
    assert "network.delay" in fault_names  # from missing-timeout
    assert "pod.kill" in fault_names  # from single-replica


def test_static_hypothesizer_returns_empty_for_clean_code(tmp_path: Path) -> None:
    code = _make_repo(tmp_path, {
        "src/clean.py": "def add(a, b): return a + b\n",
    })
    h = StaticHypothesizer()
    hyps = asyncio.run(h.generate(target_app="x", target_repo=None, code=code))
    assert hyps == []


def test_static_hypothesizer_returns_empty_without_code() -> None:
    h = StaticHypothesizer()
    hyps = asyncio.run(h.generate(target_app="x", target_repo=None, code=None))
    assert hyps == []


def test_static_hypotheses_validate_against_contract(tmp_path: Path) -> None:
    """Every produced Hypothesis must validate; ids must match the regex; faults
    must be in the catalogue."""
    from agents.chaos.faults._meta import CATALOGUE

    code = _make_repo(tmp_path, {
        "src/api.py": "import requests\nrequests.get('x')\n",
        "k8s/d.yaml": "kind: Deployment\nspec:\n  replicas: 1\n",
        "src/cfg.py": 'API_KEY = "sk_live_abcdefghijklmnop"\n',
    })
    hyps = asyncio.run(StaticHypothesizer().generate(
        target_app="x", target_repo=None, code=code,
    ))
    assert hyps  # produced at least one
    for hyp in hyps:
        # Pydantic already validated; this asserts catalogue mapping.
        assert hyp.proposed_fault in CATALOGUE, hyp.proposed_fault
        assert hyp.code_references, hyp.id


def test_static_hypothesizer_with_custom_detectors(tmp_path: Path) -> None:
    """Pass a single-detector list — only that detector runs."""
    code = _make_repo(tmp_path, {
        "k8s/d.yaml": "kind: Deployment\nspec:\n  replicas: 1\n",
        "src/api.py": "import requests\nrequests.get('x')\n",
    })
    h = StaticHypothesizer(detectors=[SingleReplicaDetector()])
    hyps = asyncio.run(h.generate(target_app="x", target_repo=None, code=code))
    # Only single-replica fired even though missing-timeout would have matched.
    assert len(hyps) == 1
    assert hyps[0].proposed_fault == "pod.kill"


@pytest.mark.parametrize(
    "detector_class,fault",
    [
        (MissingTimeoutDetector, "network.delay"),
        (MissingRetryDetector, "network.loss"),
        (MissingCircuitBreakerDetector, "network.partition"),
        (NoFallbackForCacheDetector, "pod.kill"),
        (SyncCallInAsyncDetector, "network.delay"),
        (SingleReplicaDetector, "pod.kill"),
        (HardPodAffinityDetector, "pod.kill"),
        (HardcodedSecretDetector, "secret.rotate"),
    ],
)
def test_each_detector_maps_to_a_catalogue_fault(detector_class, fault) -> None:
    """Schema check: every detector's configured fault must exist in the catalogue."""
    from agents.chaos.faults._meta import CATALOGUE
    from agents.tester.hypothesizer import _DETECTOR_CONFIG

    cfg = _DETECTOR_CONFIG[detector_class().name]
    assert cfg["fault"] == fault
    assert cfg["fault"] in CATALOGUE
