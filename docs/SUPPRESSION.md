# Hypothesis suppression

> *"15 minutes to diagnose a 6-month-old known issue is the kind of thing
> that gets a system uninstalled."* — Oleg Okner

The loop is allowed to be confidently wrong about things the operator has
already accepted as wontfix. Without a way to muzzle it, every run wastes
time re-discovering issues that are tracked, deferred, or known false
positives. **Suppression** mutes a diagnosis hypothesis from triggering the
fixer — the hypothesis is still recorded in the audit trail, the
orchestrator just skips `propose_fix` for it.

## Where rules live

| Source | Path | When to use |
|---|---|---|
| **Repo-level** | `.chaos/suppress.yaml` at the repo root | Standing rules every plan in this repo inherits. Commit it. |
| **Plan-level** | `suppress:` field on the YAML plan | One-off rules for a specific experiment plan. |

The two are merged at run-time (repo first, then plan). The first rule that
matches a given hypothesis wins, and its `reason` is what lands in the
audit trail.

## Matching

A rule needs **at least one** of these match fields. Multiple fields on a
single rule are an AND.

| Field | Match | Example |
|---|---|---|
| `hypothesis_id` | Stable 12-hex fingerprint of the hypothesis | `a1b2c3d4e5f6` |
| `fix_class` | Equals `RootCauseHypothesis.suggested_fix_class` | `missing-retry` |
| `path_glob` | fnmatch glob against any entry in `affected_paths` | `services/legacy/*` |
| `summary_contains` | Case-insensitive substring in the summary | `hardcoded secret` |

Optional fields on every rule:

- `reason` — free-text label preserved in `diagnosis.suppression_notes` for
  the audit trail. Use this for "tracked in JIRA-1234" style breadcrumbs.
- `expires_at` — ISO datetime; the rule stops matching after that point.
  Use this so stale suppressions don't accumulate forever.

## Example

```yaml
# .chaos/suppress.yaml
rules:
  - path_glob: services/legacy/*
    reason: legacy module, slated for rewrite in 2027-Q1

  - fix_class: missing-retry
    path_glob: vendor/**
    reason: vendor code — escalation tracked in JIRA-1234

  - summary_contains: hardcoded secret
    path_glob: tests/fixtures/**
    reason: false-positive on test fixtures
    expires_at: 2026-08-01T00:00:00Z
```

Inline on a plan:

```yaml
# experiments/examples/01-redis-network-loss.yaml
experiment_id: exp-000000000001
title: "OTel cartservice tolerates valkey-cart network loss"
# ... rest of plan ...
suppress:
  - fix_class: missing-circuit-breaker
    reason: deferred to next quarter
```

## What happens when a hypothesis is suppressed

1. The diagnostician runs and produces hypotheses normally.
2. The orchestrator computes the active suppress list (repo + plan).
3. For each hypothesis, the first matching rule (if any) is recorded:
   - `diagnosis.suppressed_fingerprints` gets the 12-hex fingerprint
   - `diagnosis.suppression_notes` maps fingerprint → rule reason
4. If **every** hypothesis is suppressed, the orchestrator skips
   `propose_fix` entirely. The record's `fix_proposal` stays `None` and
   the diagnosis's `notes` field records the decline.
5. If **some** are suppressed and some active, the fixer runs against the
   active ones only.

The hypothesis list itself is **never modified**. The suppression is a tag,
not a delete — the audit trail keeps every receipt.

## Getting a `hypothesis_id`

Run the loop once to produce a hypothesis. Each hypothesis carries its
own 12-hex `id` field — that's the fingerprint. The CLI surfaces it
directly:

```bash
chaos show <experiment-id> | jq '.diagnosis.hypotheses[].id'
```

## CLI shortcuts

Two commands make managing rules painless:

```bash
# Append a rule muting hypothesis #1 from a recorded experiment:
chaos suppress add exp-aaaaaaaaaaaa 1 --reason "tracked in JIRA-1234"

# Add a sunset:
chaos suppress add exp-aaaaaaaaaaaa 1 \
    --reason "false-positive; revisit after detector tuning" \
    --expires 2026-08-01T00:00:00Z

# List active rules:
chaos suppress list
# match                          reason                  expires_at
# hypothesis_id='22681744a18a'   tracked in JIRA-1234    —
```

`chaos suppress add` reads the hypothesis at the given 1-based index,
takes its `id`, and appends a rule to `<cwd>/.chaos/suppress.yaml`
(creating the file if it doesn't exist). The file format is the same
one you'd hand-write — `chaos suppress list` and the orchestrator both
read it identically.

If the diagnostician's summary changes for a finding, the fingerprint
changes too — the operator must re-suppress. That's intentional: a different
summary is a different finding. Use `fix_class` + `path_glob` together for
suppressions that should survive summary drift.

## See also

- [`.chaos/suppress.yaml.example`](../.chaos/suppress.yaml.example) — copy-and-edit template
- [`orchestrator/suppression.py`](../orchestrator/suppression.py) — evaluation logic
- [`shared/contracts.py`](../shared/contracts.py) — `SuppressionRule` schema
