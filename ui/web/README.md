# @chaos/ui-web

Angular 21 standalone + Signals SPA for the chaos diagnostic UI.
Material 3 for primitives, ECharts for the cross-experiment dashboards,
zoneless change detection.

This is half of a pnpm workspace. **Run pnpm from `ui/`, not from here.**
For the full walkthrough — every page with a screenshot, the wiring to
the orchestrator, and the production deploy notes — see
[`../README.md`](../README.md).

## In one terminal

```bash
# from <repo>/ui
pnpm install                            # one-time
pnpm --filter @chaos/ui-web start       # http://localhost:4200
```

The dev server proxies `/api/*` to the NestJS server at
`127.0.0.1:3000` via `proxy.conf.json`, so the browser only ever sees
one origin.

## Routes

`/` dashboard · `/experiments(/:id)?` list + detail with 6 tabs ·
`/llm`, `/findings`, `/fixes` cross-experiment ECharts pages · `/run`
control plane.

## Test

```bash
pnpm --filter @chaos/ui-web test        # 36 vitest tests, zoneless
pnpm --filter @chaos/ui-web build       # ng build → dist/web/
```
