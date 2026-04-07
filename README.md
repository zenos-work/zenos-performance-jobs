# zenos-jobs

Cloudflare Worker project for scheduled background jobs in Zenos.

Runtime: Python Worker (`src/index.py`).

Note on `node_modules`:
- Runtime is Python-only, but local developer tooling still uses Wrangler CLI.
- If you run `npm install`, `node_modules` appears only for tooling (deploy/dev command), not for Worker runtime logic.
- In CI deploy workflow, Wrangler is executed through GitHub Action and does not require keeping TypeScript source files.

## Jobs Included

1. `cache-warm`
- Runs as service-specific schedules (different frequencies per service).
- Pre-fetches frontend/backend endpoints and stores them in edge cache.
- Improves server-side response latency and cache hit rates.

Default service schedules (UTC):
- `core` => `*/10 * * * *` (every 10 min)
- `discovery` => `*/30 * * * *` (every 30 min)
- `social` => `15 * * * *` (hourly at minute 15)
- `admin` => `45 * * * *` (hourly at minute 45)

2. `weekly-e2e`
- Runs every Thursday 00:00 IST.
- Cron expression in Cloudflare (UTC): `30 18 * * 3`.
- Triggers `zenos-e2e/scripts/run-e2e.sh --ci` through a configured runner webhook.
- Sends summary report email to addresses in `END_TO_END_REPORT_RECEIVERS`.

## Why Thursday 00:00 IST maps to Wednesday UTC

Cloudflare cron uses UTC. IST is UTC+5:30.

- Thursday 00:00 IST
- Wednesday 18:30 UTC
- Cron: `30 18 * * 3`

## Endpoints

- `GET /health`
- `GET /jobs/run?job=cache-warm&service=core`
- `GET /jobs/run?job=cache-warm&service=discovery`
- `GET /jobs/run?job=cache-warm&service=social`
- `GET /jobs/run?job=cache-warm&service=admin`
- `GET /jobs/run?job=weekly-e2e`
- `POST /notify` (send custom HTML/text report email payload)

Manual endpoints are useful for smoke testing after deploy.

## Environment Variables

### Required for cache warming
- `FRONTEND_BASE_URL`
- `API_BASE_URL`

### Optional for cache warming
- `CACHE_WARM_URLS`: comma/semicolon/newline separated URLs. If empty, defaults are used.
- `CACHE_WARM_URLS_CORE`
- `CACHE_WARM_URLS_DISCOVERY`
- `CACHE_WARM_URLS_SOCIAL`
- `CACHE_WARM_URLS_ADMIN`
- `CACHE_TTL_SECONDS`
- `CACHE_WARM_TIMEOUT_MS`

### Schedule frequency per service
- `CACHE_WARM_CRON_CORE`
- `CACHE_WARM_CRON_DISCOVERY`
- `CACHE_WARM_CRON_SOCIAL`
- `CACHE_WARM_CRON_ADMIN`
- `E2E_WEEKLY_CRON`

### Required for weekly e2e trigger
- `E2E_RUNNER_WEBHOOK_URL`: endpoint that executes `./scripts/run-e2e.sh --ci` in `zenos-e2e` and returns JSON.

### Optional auth for weekly e2e trigger
- `E2E_RUNNER_AUTH_HEADER`
- `E2E_RUNNER_AUTH_TOKEN`

### Required for report receivers
- `END_TO_END_REPORT_RECEIVERS`: comma/semicolon/newline-separated emails.
- `NOTIFY_SECRET`: shared secret required by `POST /notify`.

### Email provider options
Use one provider:
1. Resend
- `RESEND_API_KEY`
- `RESEND_FROM`

2. Custom webhook
- `REPORT_EMAIL_WEBHOOK_URL`
- `REPORT_EMAIL_WEBHOOK_TOKEN`

### Optional report link
- `E2E_REPORT_BASE_URL`: used with `runId` when runner response does not include `reportUrl`.

## Expected Runner Webhook Contract

`POST E2E_RUNNER_WEBHOOK_URL`

Request payload:
```json
{
  "source": "zenos-jobs",
  "task": "weekly-e2e",
  "command": "./scripts/run-e2e.sh --ci",
  "requestedAt": "2026-04-05T00:00:00.000Z",
  "timezone": "Asia/Kolkata",
  "schedule": "Thursday 00:00 IST"
}
```

Response payload (example):
```json
{
  "ok": true,
  "status": "success",
  "runId": "run-20260405-235959",
  "reportUrl": "https://reports.zenos.work/e2e/run-20260405-235959",
  "summary": "passed=378, failed=0, skipped=17, durationMinutes=29.2",
  "metrics": {
    "passed": 378,
    "failed": 0,
    "skipped": 17,
    "durationMinutes": 29.2
  }
}
```

## Local Development

```bash
cd /mnt/ai-enterprise-machine-shared-disk/projects/zenos/zenos-jobs
npm install
cp .dev.vars.example .dev.vars
npm run dev
```

## Deploy

```bash
npm run deploy
```

## CI/CD Pipeline

Deployment workflow is in `.github/workflows/deploy.yml`.

- Triggered on push to `development`/`main` for Worker source/config changes.
- Deploys Python Worker directly via `cloudflare/wrangler-action`.
- Uses GitHub secrets:
  - `CLOUDFLARE_API_TOKEN`
  - `CLOUDFLARE_ACCOUNT_ID`

## Cloudflare Secrets Setup

```bash
wrangler secret put RESEND_API_KEY
wrangler secret put E2E_RUNNER_AUTH_TOKEN
wrangler secret put REPORT_EMAIL_WEBHOOK_TOKEN
```

## Suggested Production Setup

1. Deploy this worker (`zenos-jobs`).
2. Configure a secure runner endpoint in your cloud environment that can execute:
  - `cd /mnt/ai-enterprise-machine-shared-disk/projects/zenos/zenos-e2e`
  - `./scripts/run-e2e.sh --ci`
3. Configure `END_TO_END_REPORT_RECEIVERS` with all email recipients.
4. Configure one email provider (Resend or webhook).
5. Trigger `GET /jobs/run?job=weekly-e2e` once to verify report email flow.
