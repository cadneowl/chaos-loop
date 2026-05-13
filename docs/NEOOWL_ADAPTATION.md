# NeoOwl adaptation plan

> *Extending the chaos-loop to break ESP32s, RF links, and battery rails
> with the same rigor we currently break Kubernetes pods.*

NeoOwl's pitch — *"every other security system is built on the assumption
that WiFi will be there"* — is a chaos-engineering claim by another name.
This document scopes how to wire chaos-loop into the neoowl-defense
codebase so every release can produce receipts for that claim.

The chaos-loop architecture is domain-agnostic. The work is in **three
Protocol implementations + one fault catalogue extension + safety hardening
appropriate for live electronics**. No state-machine changes. No
meta-harness changes. No UI changes (the operator clicks the same Pause
button whether the experiment is breaking a Kubernetes pod or a 3.3V
regulator).

---

## What carries over for free

| chaos-loop piece | Why it works unchanged for NeoOwl |
|---|---|
| Deterministic state machine + safety gates | Audit-friendly; defense procurement loves a state diagram with abort conditions baked in. |
| Meta-harness with per-invocation audit trail | Every test run produces a forensic record in SQLite. *"Prove this test ran against firmware build `abc123` with these inputs and got this result"* answers itself. |
| Strategy modes (`static` / `hybrid` / `llm`) | Static-only is $0 and certifiable. LLM augments when an engineer is at the console. Procurement happy either way. |
| `.chaos/suppress.yaml` + Pause / Resume / Abort | A field tech can mute known firmware quirks during a customer demo without anyone touching code. |
| Draft PRs only, never auto-merges | Defense reviewers will not accept anything else. |
| `chaos suppress add` CLI + UI muted badge | Same workflow, same trust model. |

---

## What needs swapping

Three concrete Protocol implementations + one catalogue:

```
┌────────────────────────────────────────────────────────────────────┐
│  agents/chaos/                                                      │
│  ├── cluster.py        ClusterIO   ←  apply / list / delete CRDs    │
│  └── hardware.py       HardwareIO  ←  NEW: serial / HTTP / MQTT     │
│                                       to device-under-test (DUT)    │
│                                       + attack-device commands      │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  agents/chaos/faults/                                               │
│  ├── network.py        existing — Chaos Mesh CRD renderers          │
│  ├── pod.py            existing                                     │
│  ├── cert.py           existing                                     │
│  ├── rf.py             NEW: wifi.deauth, ble.flood, lora.jam        │
│  ├── power.py          NEW: brownout, ramp, supply.cut              │
│  ├── sensor.py         NEW: sensor.dropout, sensor.stuck            │
│  └── time.py           NEW: ntp.cut, clock.drift                    │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│  agents/tester/probes/                                              │
│  ├── otel-demo.yaml    existing — Prometheus queries                │
│  └── neoowl.yaml       NEW: telemetry endpoint queries              │
│                            (detector_latency_ms, gateway_rtt_ms,    │
│                             sensor_freshness_s, battery_soc,        │
│                             cert_validity_remaining_h)              │
└────────────────────────────────────────────────────────────────────┘
```

---

## The HardwareIO Protocol

Mirrors `ClusterIO`'s shape but bottoms out at a hardware bench instead of
the Kubernetes API:

```python
class HardwareIO(Protocol):
    """Talks to one device-under-test + one attack device for fault injection."""

    async def device_info(self) -> DeviceInfo:
        """Firmware version, hardware revision, MAC, serial, mode."""

    async def read_telemetry(self, metric: str) -> Sample:
        """One-shot read of a metric (sensor_freshness_s, battery_soc, ...).
        Maps to the existing `Probe` system in the tester."""

    async def inject_fault(self, fault: FaultSpec) -> InjectionHandle:
        """Hand a fault to the attack device. Returns a handle so cleanup
        is deterministic — same contract as ClusterIO.apply()."""

    async def cleanup(self, handle: InjectionHandle) -> None:
        """Tear down the fault. Idempotent — repeated calls are safe."""

    async def reset(self) -> None:
        """Hard-reset the DUT. Used before BASELINE and after ABORTED."""
```

Two concrete implementations to write:

1. **`HilHardwareIO`** — talks to a hardware-in-the-loop bench. DUT and
   attack device on a USB hub; bench script over serial. Used for nightly
   regression in CI.
2. **`SimulatedHardwareIO`** — talks to a software simulator of the DUT
   firmware. Used for dev-laptop work where you can't afford to keep an
   ESP32 plugged in.

Both expose the same Protocol. The orchestrator can't tell them apart —
same way `FakeCluster` and `KubernetesCluster` are interchangeable today.

---

## The fault catalogue — what to break, how

Each entry lives in `agents/chaos/faults/_meta.py` with metadata, and gets
a renderer in `agents/chaos/faults/<category>.py` that returns the command
sequence the attack device executes.

| Category | `name` | What the attack device does | Probe that detects it |
|---|---|---|---|
| **rf** | `wifi.deauth` | Broadcast 802.11 deauth frames at the DUT's BSSID for `duration_seconds` | `detector_latency_ms`, `gateway_uplink_rtt_ms` |
| **rf** | `wifi.jam` | Sweep a noise signal across 2.4 GHz | `wifi_rssi_dbm`, `detector_latency_ms` |
| **rf** | `ble.advertising_flood` | 10k BLE adv packets/sec from N MAC addresses | `ble_scan_queue_depth`, `cpu_idle_pct` |
| **rf** | `lora.jam` | Continuous LoRa carrier at the DUT's channel | `lora_packet_loss_pct` |
| **power** | `brownout` | Drop supply rail to N millivolts for `duration_seconds` | `boot_count_delta`, `nvs_write_failures` |
| **power** | `ramp` | Slow ramp 5V → 2.5V over 30s | `brownout_detection_fired` |
| **power** | `cut` | Hard power cut (capacitor backup test) | `event_buffer_persisted_count` |
| **sensor** | `dropout` | Disconnect a sensor's I²C/SPI line | `mesh_consensus_degraded_count` |
| **sensor** | `stuck` | Intercept the bus, replay last reading | `anomaly_detector_fired` |
| **time** | `ntp.cut` | Firewall NTP traffic for the gateway | `cert_renewal_deferred_count` |
| **time** | `clock.drift` | Inject a fake NTP server with skew | `cert_validation_failures` |

Each renderer returns a `FaultPlan` (existing struct) that
`HardwareIO.inject_fault` executes against the attack device.

---

## Probes — what "steady state" means for a wireless sensor

`neoowl-sensors` already emits telemetry; the tester just needs the
queries.

```yaml
# agents/tester/probes/neoowl.yaml
probes:
  - name: detector_latency_p95_ms
    description: "Time from jammer-active to detection event, p95"
    query: 'histogram_quantile(0.95, rate(detector_latency_ms_bucket[5m]))'
    expect: { kind: value_below, threshold: 2000 }

  - name: gateway_uplink_rtt_ms
    description: "Round-trip to cloud control plane"
    query: 'gateway_uplink_rtt_ms{deployment="$deployment_id"}'
    expect: { kind: value_below, threshold: 1500 }

  - name: sensor_freshness_s
    description: "Time since the most recent reading from any active sensor"
    query: 'max(time() - sensor_last_seen_ts) by (sensor_id)'
    expect: { kind: value_below, threshold: 30 }

  - name: cert_validity_remaining_h
    description: "Hours until any cert in the deployment expires"
    query: 'min(cert_not_after_ts - time()) / 3600'
    expect: { kind: value_above, threshold: 168 }  # one week

  - name: detector_false_positive_rate
    description: "Detections / known-quiet windows over the last hour"
    query: 'rate(detector_event_total{kind="jammer"}[1h]) / on() rate(known_quiet_window_seconds[1h])'
    expect: { kind: value_below, threshold: 0.01 }
```

---

## Safety gates — stricter than the cloud version

Live electronics need gates the orchestrator currently doesn't enforce:

1. **`check_bench_mode`** — the DUT must report itself as `MODE_BENCH` over
   telemetry before any fault runs. Production-mode DUTs reject all chaos
   commands at the firmware level (separate gate, also enforced by the
   orchestrator as defense in depth).
2. **`check_geofence`** — the bench has a known GPS / WiFi fingerprint; if
   the DUT reports anywhere else, abort with `CLUSTER_DENIED`-equivalent
   reason. Don't run jammer-emission tests from your kitchen.
3. **`check_thermal_headroom`** — read DUT die temp before each transition;
   abort if `> 70 °C`. Especially for `power.cut` cycles.
4. **`check_emission_compliance`** — power output for any `rf.*` fault must
   stay within the licensed-band limit declared in the plan. The renderer
   refuses to emit above the declared cap; the orchestrator double-checks.
5. **`check_battery_headroom`** — DUT battery SoC > 30% before starting,
   otherwise the brownout cascade will brick the device.

All five are deterministic Python, no LLM, audit-friendly. Same pattern as
the existing `agents/orchestrator/safety.py` gates.

---

## Phased delivery

### Phase 1 — minimum viable hardware chaos (1 week)

The smallest slice that produces a real test report against real hardware.

- [ ] `agents/chaos/cluster_io.py` extracted into a `cluster_io_protocol.py`
      so `HardwareIO` can live alongside without breaking the Mesh path.
- [ ] `agents/chaos/hardware_io.py` — `HardwareIO` Protocol + `SimulatedHardwareIO`
      (talks to a Python simulator of the DUT firmware).
- [ ] One `rf.py` fault: `wifi.deauth`. One renderer.
- [ ] One probe set: `neoowl.yaml` with `detector_latency_p95_ms` only.
- [ ] One example plan: `experiments/neoowl/01-wifi-deauth.yaml`.
- [ ] End-to-end test: simulator boots, baseline probe reads 200ms, deauth
      injected for 30s, probe reads 1.4s, verify steady-state, abort.
- [ ] Tests in `tests/test_hardware_io.py` covering the Protocol contract.

**Exit criteria**: `chaos run experiments/neoowl/01-wifi-deauth.yaml --profile static` produces a real `ExperimentRecord` against the simulated DUT.

### Phase 2 — real hardware bench (1–2 weeks)

- [ ] `HilHardwareIO` — talks to a USB-serial bench: one ESP32 as DUT, one
      as attacker.
- [ ] Attack-ESP32 firmware: minimal command interpreter on serial that
      executes one fault command at a time. Lives in
      `neoowl-defense/chaos-attacker/` (new dir).
- [ ] DUT-side telemetry endpoint reads (the existing
      `neoowl-sensors/neowl-agent-esp32` already exposes HTTP; consume it).
- [ ] Five safety gates wired into the loop (`check_bench_mode`,
      `check_geofence`, `check_thermal_headroom`,
      `check_emission_compliance`, `check_battery_headroom`).
- [ ] Three more `rf.*` faults: `wifi.jam`, `ble.advertising_flood`, `lora.jam`.

**Exit criteria**: a hardware engineer can run `chaos run …` and watch real RF + telemetry + record.

### Phase 3 — catalogue expansion + CI (1 week)

- [ ] Remaining fault categories: `power.*` (3), `sensor.*` (2), `time.*` (2).
- [ ] Five more probes covering battery / cert / gateway-uplink / mesh-consensus / false-positive-rate.
- [ ] CI integration in `neoowl-defense`: every firmware PR runs the
      catalogue against the bench. Results posted to the PR as a comment.
- [ ] Suppression rules baked into `.chaos/suppress.yaml` for any
      known-quirks-not-yet-fixed (carry forward an issue link per rule).

**Exit criteria**: a firmware regression in the deauth-detection latency budget fails the next PR build, before it ships.

### Phase 4 — eval surface (1 week)

- [ ] UI extension: a per-fault timeseries view (e.g., "deauth-detection p95 latency over the last 50 firmware builds"). Reuses the existing ECharts machinery on the `/llm`-equivalent page.
- [ ] Public claims dashboard generator: takes the last N runs of a curated
      fault set and produces a one-page PDF a salesperson can hand to a
      procurement officer.
- [ ] Markdown export of any individual experiment record for inclusion in
      a quarterly compliance pack.

**Exit criteria**: marketing can publish "our jamming-response p95 latency
over 18 months" with verifiable receipts pointing at signed SQLite blobs.

---

## Where the code lives

| Concern | Repo |
|---|---|
| `HardwareIO` Protocol, generic hardware safety gates, `power`/`sensor`/`time` fault metadata | `chaos-loop` (this repo) — domain-agnostic primitives |
| NeoOwl-specific renderers, `rf.*` faults that target NeoOwl frequencies + emissions limits, neoowl probe set, neoowl plans, attack-ESP32 firmware, claims dashboard | `neoowl-defense` — depends on `chaos-loop` as a library |

This separation lets chaos-loop pick up other hardware projects later (a
robotics startup, an industrial-IoT vendor) without forking. NeoOwl gets a
domain-specific overlay; chaos-loop stays the shared spine.

---

## Open questions for the NeoOwl team

1. **Telemetry transport** — the existing `neowl-agent-esp32` exposes an
   HTTP endpoint locally; is there a Prometheus scrape already, or should
   chaos-loop's tester learn to read your protocol directly?
2. **Bench configuration** — one bench per developer, or a shared central
   bench with queueing? Affects CI integration design.
3. **Emission certification** — which bands are you already licensed to
   transmit on for testing? Affects which `rf.*` faults can ship in v1 vs
   need RF-anechoic-chamber-only restrictions.
4. **Cert-system coupling** — the `cert-system` directory looks substantial.
   Should `time.*` faults be allowed to test cert renewal logic, or is that
   too close to production?

---

## See also

- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — chaos-loop agent specs
- [`docs/SAFETY.md`](SAFETY.md) — current safety model that the hardware gates extend
- [`docs/SUPPRESSION.md`](SUPPRESSION.md) — how `.chaos/suppress.yaml` works (unchanged)
- [`agents/chaos/cluster.py`](../agents/chaos/cluster.py) — existing `ClusterIO`, the model `HardwareIO` follows
- [`agents/chaos/faults/network.py`](../agents/chaos/faults/network.py) — existing fault renderer pattern
