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
    MissingRetryDetector,
    MissingTimeoutDetector,
    SingleReplicaDetector,
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
