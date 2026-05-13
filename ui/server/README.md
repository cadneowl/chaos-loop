# @chaos/ui-server

NestJS 11 backend for the chaos diagnostic UI. Serves a REST API
(experiments + cross-experiment aggregates + control plane) over the
orchestrator's SQLite store.

This is half of a pnpm workspace. **Run pnpm from `ui/`, not from here.**
For the full walkthrough — architecture, screenshots, environment vars,
and how to wire it to a live chaos-mesh cluster — see
[`../README.md`](../README.md).

## In one terminal

```bash
# from <repo>/ui
pnpm install                                    # one-time
pnpm --filter @chaos/ui-server start:dev        # http://127.0.0.1:3000
```

## Endpoints

`GET  /api/v1/health` · `GET /api/v1/experiments(/:id(/control)?)?` ·
`GET  /api/v1/aggregates/{llm,findings,fixes}` · `GET /api/v1/plans` ·
`POST /api/v1/experiments/:id/{pause,resume,abort}` ·
`POST /api/v1/experiments/run`

Full table with bodies + status codes:
[`../README.md#api`](../README.md#api).

## Test

```bash
pnpm --filter @chaos/ui-server test     # 50 jest tests
pnpm --filter @chaos/ui-server build    # nest build → dist/
```
