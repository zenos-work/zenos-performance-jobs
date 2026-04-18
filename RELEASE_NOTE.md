# Zenos Performance Jobs - Production Release v1.0.0

## Summary

- **What changed**: Initial production release of the Zenos Performance Jobs Cloudflare Worker, providing a unified, serverless scheduler for cache warming, notification delivery, E2E testing, and platform snapshots across the Zenos platform.
- **Why this change is needed**: Enables automated background job execution without dedicated infrastructure, reducing operational overhead while ensuring critical platform operations (cache refresh, notifications, system health checks) run reliably on schedule.
- **Scope of impact**: Platform-wide scheduled operations including content caching, notification delivery pipelines, weekly E2E smoke tests, and diagnostic snapshots. No breaking changes to existing APIs.

## Key Capabilities

### Cache Warming
- **Core**: Homepage, membership, and API health check (every 10 minutes)
- **Discovery**: Explore, search, and trending content endpoints (every 30 minutes)
- **Social**: Notifications and trending feeds (15 minutes past each hour)
- **Admin**: Admin dashboards and ranking weights (45 minutes past each hour)
- Configurable TTLs and warm targets per environment

### Notification Delivery
- Processes pending notifications every 5 minutes
- Integrates with backend notification queue
- Supports Resend email provider and custom webhooks

### E2E Testing
- Weekly smoke test trigger (Thursdays 12:00 AM IST)
- Integrates with GitHub Actions CI/CD pipeline
- Generates automated test reports with email delivery

### Platform Snapshots
- Hourly diagnostic snapshots (every 6 hours)
- Monitors feature health, API availability, cache hit rates
- Provides operational insights for platform monitoring

## Architecture

- **Runtime**: Cloudflare Workers (Python)
- **Triggers**: 4 cron expressions (consolidated from individual schedules)
- **Storage**: D1 SQLite for state (optional per job)
- **API Integration**: HTTP webhooks to backend services
- **Email**: Resend API + custom webhook support

## Consolidated Cron Schedule

| Trigger | Expression | Jobs |
|---------|-----------|------|
| 1 | `*/10 * * * *` | Cache warm (core + discovery on :00, :30) |
| 2 | `15,45 * * * *` | Cache warm (social + admin) |
| 3 | `*/5 * * * *` | Notification delivery |
| 4 | `30 18 * * 3` | Weekly E2E + platform snapshot |

## Deployment

### Prerequisites
- Cloudflare account with Workers enabled
- Wrangler CLI v3.0+
- Environment secrets configured (see Configuration section)
- Backend API endpoints accessible from Cloudflare edge

### Configuration

**Required Environment Secrets** (set via `wrangler secret put`):
- `ZENOS_SERVICE_SECRET` — Service-to-service authentication token
- `RESEND_API_KEY` — (optional) Resend email API key
- `VAPID_PRIVATE_KEY_HEX` — (optional) Web push VAPID key

**Environment Variables** (set in `wrangler.toml` per environment):
- `API_BASE_URL` — Backend API endpoint (e.g., `https://api.zenos.work`)
- `FRONTEND_BASE_URL` — Frontend base URL for cache warm targets
- `CACHE_WARM_URLS_*` — Custom cache warm target URLs per service
- `END_TO_END_REPORT_RECEIVERS` — Email recipients for E2E reports
- `E2E_RUNNER_WEBHOOK_URL` — E2E test runner webhook endpoint
- `NOTIFICATION_DELIVERY_CRON` — Notification delivery schedule override

### Deployment Impact

- [x] **Requires environment configuration** — Must set secrets and API endpoints
- [x] **Requires Cloudflare Workers setup** — Project must be deployed to CF Workers
- [x] **Requires backend integration** — Backend API must be running and accessible
- [ ] **Breaking changes** — None
- [ ] **Database migrations** — Not required
- [ ] **Manual rollout steps** — Standard Cloudflare deployment

## Validation Checklist

- [ ] All 4 cron triggers fire successfully (check Cloudflare dashboard)
- [ ] Cache warm jobs complete without 504/timeout errors
- [ ] Notification delivery processes pending notifications
- [ ] E2E test trigger calls backend webhook correctly
- [ ] Email reports arrive at configured recipients
- [ ] Health endpoint (`/health`) returns `ok: true`
- [ ] Manual job invocation works via `/jobs/run?job=<job>&service=<service>`
- [ ] Observability: Cron execution logs available in Cloudflare dashboard

## Release Labels

- [x] **feature** — New platform capability
- [ ] **fix** — Bug fix
- [ ] **security** — Security-related change
- [ ] **breaking-change** — Breaking API change
- [ ] **docs** — Documentation only
- [ ] **chore** — Infrastructure/maintenance

## Checklist

- [x] Code tested locally
- [x] Python syntax validation (`py_compile`)
- [x] Wrangler configuration validated
- [x] Environment variables documented
- [x] Consolidated to 4-cron limit (Cloudflare constraint)
- [x] No secrets committed to repository
- [x] README and inline comments updated

## Notes for Release

### User-Visible Changes
- Automated cache warming across all platform surfaces
- Reliable notification delivery without manual intervention
- Weekly automated E2E smoke tests with email reports
- Ongoing platform health monitoring via snapshots

### Risks and Mitigations
- **Risk**: Cache warm jobs fail due to API downtime → **Mitigation**: Retries with exponential backoff; alert on repeated failures
- **Risk**: Notification delivery lag during high volume → **Mitigation**: Scalable queue processing; configurable batch sizes
- **Risk**: E2E tests generate false negatives on transient failures → **Mitigation**: Smoke tests only; detailed failure logs for debugging
- **Risk**: Unsecured webhook endpoints for job triggers → **Mitigation**: Require shared secret in environment; validate before processing

### Rollback Plan
- Disable cron triggers in Cloudflare dashboard (no re-deployment needed)
- Redeploy previous worker version: `wrangler deploy --env production --name zenos-jobs`
- Restore prior environment configuration from secrets backup
- Verify health endpoint returns healthy status before re-enabling

### Future Enhancements
- Platform snapshot metrics exported to observability platform (e.g., Datadog)
- Configurable job retry policies per trigger
- Dashboard UI for job monitoring and manual trigger
- Job execution metrics and SLA tracking
- Multi-region worker deployment for lower latency

## Contact & Support

- **Deployment Issues**: Check Cloudflare Workers dashboard logs
- **Job-Specific Issues**: Review worker output in `/jobs/run?job=<name>` endpoint
- **Configuration Help**: Refer to `wrangler.toml` comments and environment setup guide
