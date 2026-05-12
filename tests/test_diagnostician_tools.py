"""Tests for the diagnostician's data tools: LokiBackend + TargetCodeReader."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agents.diagnostician.tools import (
    CodeReadError,
    FixtureLokiBackend,
    LokiQueryError,
    TargetCodeReader,
)

# ---------------------------------------------------------------------------- #
# LokiBackend                                                                  #
# ---------------------------------------------------------------------------- #


def test_loki_returns_lines_within_window() -> None:
    # Loki timestamps are nanoseconds. start/end are seconds. 1.5s == 1.5e9 ns.
    s = 1_000_000_000  # one second in ns
    backend = FixtureLokiBackend({
        '{service="cart"}': [
            {
                "labels": {"service": "cart"},
                "lines": [
                    (1 * s, "before"),
                    (2 * s, "during 1"),
                    (int(2.5 * s), "during 2"),
                    (5 * s, "way after"),
                ],
            }
        ]
    })
    lines = asyncio.run(
        backend.query_range('{service="cart"}', start=1.5, end=3.0)
    )
    # Only lines with ts in [1.5s..3.0s] => 2s and 2.5s entries.
    assert [ln.line for ln in lines] == ["during 1", "during 2"]
    assert all(ln.labels == {"service": "cart"} for ln in lines)


def test_loki_limit_truncates() -> None:
    backend = FixtureLokiBackend({
        "q": [
            {
                "labels": {"x": "y"},
                "lines": [(i * 1_000_000_000, f"line {i}") for i in range(1, 11)],
            }
        ]
    })
    lines = asyncio.run(backend.query_range("q", start=0, end=20, limit=3))
    assert len(lines) == 3


def test_loki_missing_fixture_errors() -> None:
    backend = FixtureLokiBackend()
    with pytest.raises(LokiQueryError):
        asyncio.run(backend.query_range("nope", start=0, end=1))


# ---------------------------------------------------------------------------- #
# TargetCodeReader                                                             #
# ---------------------------------------------------------------------------- #


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / "src" / "redis_client.py").write_text(
        "import redis\n\ndef get(key):\n    return redis.Redis().get(key)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text(
        "def test_hello():\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def test_code_reader_read_whole_file(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    text = cr.read_file("src/main.py")
    assert "def hello" in text


def test_code_reader_line_range(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    text = cr.read_file("src/redis_client.py", line_start=3, line_end=4)
    # 1-indexed inclusive: lines 3 and 4 of the file.
    assert "def get" in text
    assert "redis.Redis" in text
    assert "import redis" not in text  # outside range


def test_code_reader_rejects_absolute_path(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    with pytest.raises(CodeReadError, match="absolute"):
        cr.read_file("/etc/passwd")


def test_code_reader_rejects_traversal(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    with pytest.raises(CodeReadError, match="escapes target root"):
        cr.read_file("../../../etc/passwd")


def test_code_reader_missing_file(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    with pytest.raises(CodeReadError, match="not found"):
        cr.read_file("src/nonexistent.py")


def test_code_reader_list_files(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo, ignore_segments=frozenset())
    listed = cr.list_files("**/*.py")
    # Path separators differ by OS; normalize for assertion.
    paths = {p.replace("\\", "/") for p in listed}
    assert "src/main.py" in paths
    assert "src/redis_client.py" in paths
    assert "tests/test_main.py" in paths


def test_code_reader_default_ignore_segments_skip_tests_and_venv(
    fake_repo: Path, tmp_path: Path
) -> None:
    """Default reader hides tests/, .venv/, __pycache__/, etc."""
    # The fixture has tests/test_main.py — should be hidden by default.
    (fake_repo / ".venv").mkdir()
    (fake_repo / ".venv" / "junk.py").write_text("x = 1\n")
    (fake_repo / "src" / "__pycache__").mkdir()
    (fake_repo / "src" / "__pycache__" / "main.cpython-313.pyc").write_text("\x00")

    cr = TargetCodeReader(fake_repo)  # default ignores
    paths = {p.replace("\\", "/") for p in cr.list_files("**/*.py")}
    assert "src/main.py" in paths
    assert "tests/test_main.py" not in paths
    assert ".venv/junk.py" not in paths


def test_code_reader_read_file_respects_ignore(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)  # default ignores tests/
    with pytest.raises(CodeReadError, match="ignored"):
        cr.read_file("tests/test_main.py")


def test_code_reader_grep_respects_ignore(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)  # default ignores tests/
    hits = cr.grep("test_hello")  # text only in tests/test_main.py
    assert hits == []


def test_code_reader_grep(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    hits = cr.grep("redis", glob="src/**/*.py")
    # Only redis_client.py mentions redis.
    paths = {h[0].replace("\\", "/") for h in hits}
    assert "src/redis_client.py" in paths
    assert "src/main.py" not in paths


def test_code_reader_grep_bad_regex(fake_repo: Path) -> None:
    cr = TargetCodeReader(fake_repo)
    with pytest.raises(CodeReadError, match="invalid regex"):
        cr.grep("[unclosed", glob="**/*.py")


def test_code_reader_init_rejects_nonexistent_root(tmp_path: Path) -> None:
    with pytest.raises(CodeReadError, match="does not exist"):
        TargetCodeReader(tmp_path / "nope")


def test_code_reader_init_rejects_file_as_root(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(CodeReadError, match="not a directory"):
        TargetCodeReader(f)
