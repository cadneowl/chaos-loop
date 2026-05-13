"""Tests for HilHardwareIO + transports (no real hardware needed)."""

from __future__ import annotations

import asyncio
import json

import pytest

from agents.chaos.hardware_io import HardwareFault, HilHardwareIO, HilTransportError
from agents.chaos.hil_transport import FakeHttpTransport, FakeSerialTransport


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


# ---------------------------------------------------------------- helpers


def _io_with(
    *, serial_responses: list[dict] | None = None, http_responses: dict | None = None
) -> tuple[HilHardwareIO, FakeSerialTransport, FakeHttpTransport]:
    attacker = FakeSerialTransport(
        recv_queue=[json.dumps(r) for r in (serial_responses or [])]
    )
    dut = FakeHttpTransport(responses=http_responses or {})
    io = HilHardwareIO(attacker=attacker, dut=dut)
    return io, attacker, dut


# ---------------------------------------------------------------- device_info


def test_device_info_parses_attacker_reply() -> None:
    io, attacker, _ = _io_with(
        serial_responses=[
            {
                "ok": True,
                "firmware": "1.2.3",
                "serial": "AT-001",
                "hardware": "rev-B",
                "mode": "BENCH",
            }
        ]
    )
    info = _run(io.device_info())
    assert info.serial == "AT-001"
    assert info.firmware_version == "1.2.3"
    assert info.hardware_revision == "rev-B"
    assert info.mode == "BENCH"
    # Verify the wire-format request shape.
    assert json.loads(attacker.sent[0]) == {"cmd": "info"}


def test_device_info_raises_on_attacker_error() -> None:
    io, _, _ = _io_with(
        serial_responses=[{"ok": False, "error": "ESP32 not ready"}]
    )
    with pytest.raises(HilTransportError, match="not ready"):
        _run(io.device_info())


# ---------------------------------------------------------------- telemetry


def test_read_telemetry_hits_the_dut_endpoint() -> None:
    io, _, dut = _io_with(
        http_responses={
            "http://localhost:8080/telemetry/detector_latency_p95_ms": {
                "metric": "detector_latency_p95_ms",
                "value": 234.5,
                "labels": {"device": "dut-1"},
            }
        }
    )
    sample = _run(io.read_telemetry("detector_latency_p95_ms"))
    assert sample.metric == "detector_latency_p95_ms"
    assert sample.value == 234.5
    assert sample.labels["device"] == "dut-1"
    assert dut.request_log == [
        "http://localhost:8080/telemetry/detector_latency_p95_ms"
    ]


def test_read_telemetry_uses_configured_base_url() -> None:
    """An operator may point the bench at a non-default base URL — the IO
    must respect it."""
    io = HilHardwareIO(
        attacker=FakeSerialTransport(),
        dut=FakeHttpTransport(
            responses={
                "http://10.0.0.42/v2/telemetry/battery_soc": {
                    "metric": "battery_soc",
                    "value": 0.81,
                }
            }
        ),
        telemetry_base_url="http://10.0.0.42/v2/telemetry",
    )
    sample = _run(io.read_telemetry("battery_soc"))
    assert sample.value == 0.81


# ---------------------------------------------------------------- inject + cleanup


def test_inject_fault_sends_full_command_and_returns_handle() -> None:
    io, attacker, _ = _io_with(
        serial_responses=[{"ok": True, "handle": "h-0042"}]
    )
    fault = HardwareFault(
        name="wifi.deauth",
        parameters={"target_bssid": "auto", "intensity": "high"},
        duration_seconds=30,
    )
    handle = _run(io.inject_fault(fault))
    assert handle.id == "h-0042"
    sent = json.loads(attacker.sent[0])
    assert sent == {
        "cmd": "inject",
        "fault": "wifi.deauth",
        "params": {"intensity": "high", "target_bssid": "auto"},
        "duration_seconds": 30,
    }


def test_inject_fault_raises_when_attacker_refuses() -> None:
    io, _, _ = _io_with(
        serial_responses=[{"ok": False, "error": "channel busy"}]
    )
    fault = HardwareFault(name="wifi.deauth", parameters={}, duration_seconds=1)
    with pytest.raises(HilTransportError, match="channel busy"):
        _run(io.inject_fault(fault))


def test_cleanup_treats_already_gone_as_success() -> None:
    """The attacker may report `already gone` when a fault timed out on its
    own before we got back to cleanup — that's the idempotent path."""
    io, _, _ = _io_with(
        serial_responses=[{"ok": False, "error": "handle already gone"}]
    )
    from agents.chaos.hardware_io import InjectionHandle

    _run(io.cleanup(InjectionHandle(id="h-0042")))  # must not raise


def test_cleanup_raises_on_other_errors() -> None:
    io, _, _ = _io_with(
        serial_responses=[{"ok": False, "error": "transport stalled"}]
    )
    from agents.chaos.hardware_io import InjectionHandle

    with pytest.raises(HilTransportError, match="transport stalled"):
        _run(io.cleanup(InjectionHandle(id="h-0042")))


def test_reset_sends_command_and_succeeds() -> None:
    io, attacker, _ = _io_with(serial_responses=[{"ok": True}])
    _run(io.reset())
    assert json.loads(attacker.sent[0]) == {"cmd": "reset"}


# ---------------------------------------------------------------- transport fakes


def test_fake_serial_round_trip() -> None:
    transport = FakeSerialTransport()
    transport.recv_queue.append("{\"ok\":true}")
    _run(transport.send_line('{"cmd":"info"}'))
    assert transport.sent == ['{"cmd":"info"}']
    assert _run(transport.recv_line()) == '{"ok":true}'


def test_fake_serial_responder_pairs_requests_and_replies() -> None:
    async def responder(line: str) -> str:
        if "info" in line:
            return '{"ok":true,"serial":"sim"}'
        return '{"ok":false,"error":"unknown"}'

    transport = FakeSerialTransport(responder=responder)
    _run(transport.send_line('{"cmd":"info"}'))
    assert _run(transport.recv_line()) == '{"ok":true,"serial":"sim"}'


def test_fake_serial_recv_timeout_when_no_data() -> None:
    transport = FakeSerialTransport()
    with pytest.raises(TimeoutError):
        _run(transport.recv_line(timeout_s=0.05))


def test_fake_http_records_request_and_returns_static_payload() -> None:
    transport = FakeHttpTransport(responses={"http://x/y": {"v": 1}})
    result = _run(transport.get_json("http://x/y"))
    assert result == {"v": 1}
    assert transport.request_log == ["http://x/y"]


def test_fake_http_raises_on_unmapped_url() -> None:
    transport = FakeHttpTransport(responses={})
    with pytest.raises(KeyError, match="no response for"):
        _run(transport.get_json("http://nope"))
