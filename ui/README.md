# chaos · reports UI

A read-only diagnostic UI on top of the orchestrator's SQLite store, plus a
small control plane for start / pause / resume / abort on running
experiments.

```
ui/
├── server/   Node 22 + NestJS 11 · REST + WebSocket · reads experiments.sqlite
└── web/      Angular 21 standalone + Signals · SPA
```

This is a **pnpm workspace**. Always run pnpm from `ui/`, not from `ui/server`
or `ui/web` directly.

## Status

Phase 0 — empty skeleton with a working health endpoint. No domain code yet.

| Half | Status |
|---|---|
| `server` | NestJS app with `GET /api/v1/health`, 3 unit tests |
| `web` | Angular 21 standalone app with a shell page, 3 unit tests |
| CI | `ui` job runs both halves' build + tests on every push |

Phase 1 (experiments list + detail + LLM telemetry pages) is the next PR.

## Develop

One-time:

```bash
# from repo root
cd ui
pnpm install
```

Run both halves locally (in separate terminals):

```bash
# terminal 1 — backend on http://127.0.0.1:3000
pnpm --filter @chaos/ui-server start:dev

# terminal 2 — frontend on http://localhost:4200
pnpm --filter @chaos/ui-web start
```

Healthcheck:

```bash
curl http://127.0.0.1:3000/api/v1/health
# {"status":"ok","service":"chaos-ui-server","startedAt":"…","now":"…"}
```

## Test

```bash
pnpm --filter @chaos/ui-server test
pnpm --filter @chaos/ui-web    test
```

Both halves run on CI on every push under the `ui` job in
`.github/workflows/ci.yml`.

## Build (production)

```bash
pnpm --filter @chaos/ui-server build   # → ui/server/dist/
pnpm --filter @chaos/ui-web    build   # → ui/web/dist/web/
```

## Environment

| Var | Default | Purpose |
|---|---|---|
| `CHAOS_UI_PORT` | `3000` | Server port |
| `CHAOS_UI_BIND` | `127.0.0.1` | Bind address. **Local-only by default**; set to `0.0.0.0` only when you've also set `CHAOS_UI_API_KEY` |
| `CHAOS_UI_API_KEY` | unset | Optional bearer-token auth. Required when binding to anything other than localhost (Phase 1 will enforce) |
| `CHAOS_STORE_PATH` | `~/.local/share/chaos/experiments.sqlite` | SQLite store the Python orchestrator writes to (Phase 1) |

## Why this stack

- **NestJS** mirrors Angular's module / DI / decorator structure — same
  architectural taste on both ends.
- **Angular 21** standalone + Signals — current at time of writing,
  forward-compatible API surface.
- **pnpm** — deterministic, fast, hard about phantom deps. Workspace mode
  unifies the two halves under one lockfile so a schema change can land in
  both packages atomically.
- **better-sqlite3** (Phase 1) — synchronous, zero-overhead reads. The
  store is single-writer (Python) + multi-reader (Node); WAL mode handles
  concurrency.

## See also

- [`docs/CAST.md`](../docs/CAST.md) — the seven characters the UI surfaces.
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting.
- [`docs/SAFETY.md`](../docs/SAFETY.md) — the safety properties the UI must
  not subvert.
