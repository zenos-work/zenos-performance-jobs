import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from workers import WorkerEntrypoint, Response, fetch


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_response(data, status: int = 200) -> Response:
    return Response(
        json.dumps(data, indent=2),
        status=status,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def _env_str(env, key: str, default: str = "") -> str:
    value = getattr(env, key, None)
    if value is None:
        return default
    return str(value)


def _has_valid_secret(request, expected: str) -> bool:
    if not expected:
        return False
    auth = request.headers.get("authorization")
    bearer = ""
    if auth and auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
    header = (request.headers.get("x-trigger-secret") or "").strip()
    return bearer == expected or header == expected


def _to_positive_int(raw: str, fallback: int) -> int:
    try:
        parsed = int(raw)
        if parsed > 0:
            return parsed
    except Exception:
        pass
    return fallback


def _split_urls(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for token in raw.replace(";", "\n").replace(",", "\n").split("\n"):
        token = token.strip()
        if token:
            out.append(token)
    return out


def _join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def _cache_urls_for_service(env, service: str) -> list[str]:
    frontend = _env_str(env, "FRONTEND_BASE_URL", "https://zenos.work")
    api = _env_str(env, "API_BASE_URL", "https://api.zenos.work")

    global_urls = _split_urls(_env_str(env, "CACHE_WARM_URLS", ""))
    if service == "all" and global_urls:
        return global_urls

    explicit = {
        "core": _split_urls(_env_str(env, "CACHE_WARM_URLS_CORE", "")),
        "discovery": _split_urls(_env_str(env, "CACHE_WARM_URLS_DISCOVERY", "")),
        "social": _split_urls(_env_str(env, "CACHE_WARM_URLS_SOCIAL", "")),
        "admin": _split_urls(_env_str(env, "CACHE_WARM_URLS_ADMIN", "")),
    }

    defaults = {
        "core": [
            _join_url(frontend, "/"),
            _join_url(frontend, "/membership"),
            _join_url(frontend, "/terms"),
            _join_url(api, "/health"),
        ],
        "discovery": [
            _join_url(frontend, "/explore"),
            _join_url(frontend, "/search"),
            _join_url(api, "/api/articles?sort=latest&limit=20"),
            _join_url(api, "/api/articles?sort=trending&limit=20"),
            _join_url(api, "/api/tags"),
        ],
        "social": [
            _join_url(api, "/api/articles?sort=latest&limit=20"),
            _join_url(api, "/api/articles?sort=trending&limit=20"),
            _join_url(api, "/api/notifications"),
        ],
        "admin": [
            _join_url(api, "/api/admin/stats"),
            _join_url(api, "/api/admin/ranking-weights"),
            _join_url(api, "/api/admin/content-types"),
            _join_url(api, "/api/admin/success-signals"),
        ],
    }

    def resolve(name: str) -> list[str]:
        return explicit[name] if explicit[name] else defaults[name]

    if service == "all":
        combined = resolve("core") + resolve("discovery") + resolve("social") + resolve("admin")
        dedup = []
        seen = set()
        for url in combined:
            if url in seen:
                continue
            seen.add(url)
            dedup.append(url)
        return dedup

    return resolve(service)


async def _run_cache_warm_job(env, service: str = "all") -> dict:
    started_at = _now_iso()
    ttl_seconds = _to_positive_int(_env_str(env, "CACHE_TTL_SECONDS", "300"), 300)
    timeout_ms = _to_positive_int(_env_str(env, "CACHE_WARM_TIMEOUT_MS", "25000"), 25000)
    targets = _cache_urls_for_service(env, service)

    results = []
    warmed = 0
    failed = 0

    for target in targets:
        try:
            headers = {
                "x-zenos-cache-warm": "1",
                "cache-control": f"public, max-age={ttl_seconds}",
            }
            response = await fetch(
                target,
                method="GET",
                headers=headers,
                cf={"cacheEverything": True, "cacheTtl": ttl_seconds},
            )
            ok = bool(response.ok)
            if ok:
                warmed += 1
            else:
                failed += 1
            results.append({"url": target, "ok": ok, "status": int(response.status)})
        except Exception as error:
            failed += 1
            results.append({"url": target, "ok": False, "error": str(error), "timeoutMs": timeout_ms})

    finished_at = _now_iso()
    return {
        "ok": failed == 0,
        "job": "cache-warm",
        "startedAt": started_at,
        "finishedAt": finished_at,
        "summary": f"Warmed {warmed} URLs, failed {failed} (service={service}).",
        "details": {
            "service": service,
            "warmed": warmed,
            "failed": failed,
            "urls": results,
        },
    }


def _parse_receivers(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for token in raw.replace(";", "\n").replace(",", "\n").split("\n"):
        token = token.strip().lower()
        if token and "@" in token:
            out.append(token)
    # de-dup preserve order
    dedup = []
    seen = set()
    for email in out:
        if email in seen:
            continue
        seen.add(email)
        dedup.append(email)
    return dedup


async def _send_email_via_resend(env, receivers: list[str], subject: str, text: str, html: str) -> bool:
    api_key = _env_str(env, "RESEND_API_KEY", "")
    if not api_key:
        return False

    response = await fetch(
        "https://api.resend.com/emails",
        method="POST",
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
        },
        body=json.dumps(
            {
                "from": _env_str(env, "RESEND_FROM", "noreply@zenos.work"),
                "to": receivers,
                "subject": subject,
                "text": text,
                "html": html,
            }
        ),
    )
    return bool(response.ok)


async def _send_email_via_webhook(env, receivers: list[str], subject: str, text: str, html: str) -> bool:
    webhook_url = _env_str(env, "REPORT_EMAIL_WEBHOOK_URL", "")
    if not webhook_url:
        return False

    headers = {"content-type": "application/json"}
    token = _env_str(env, "REPORT_EMAIL_WEBHOOK_TOKEN", "")
    if token:
        headers["authorization"] = f"Bearer {token}"

    response = await fetch(
        webhook_url,
        method="POST",
        headers=headers,
        body=json.dumps(
            {
                "to": receivers,
                "subject": subject,
                "text": text,
                "html": html,
            }
        ),
    )
    return bool(response.ok)


async def _send_job_report_email(env, result: dict) -> dict:
    receivers = _parse_receivers(_env_str(env, "END_TO_END_REPORT_RECEIVERS", ""))
    if not receivers:
        return {"sent": False, "provider": "none", "receivers": []}

    status = "SUCCESS" if result.get("ok") else "FAILED"
    subject = f"[{status}] zenos-jobs {result.get('job', 'unknown')}"
    details_json = json.dumps(result.get("details", {}), indent=2)
    text = (
        f"Job: {result.get('job')}\n"
        f"Status: {status}\n"
        f"Started: {result.get('startedAt')}\n"
        f"Finished: {result.get('finishedAt')}\n"
        f"Summary: {result.get('summary')}\n\n"
        f"Details:\n{details_json}\n"
    )
    html = f"<pre style=\"font-family:ui-monospace,monospace;white-space:pre-wrap;\">{text}</pre>"

    if await _send_email_via_resend(env, receivers, subject, text, html):
        return {"sent": True, "provider": "resend", "receivers": receivers}

    if await _send_email_via_webhook(env, receivers, subject, text, html):
        return {"sent": True, "provider": "webhook", "receivers": receivers}

    return {"sent": False, "provider": "none", "receivers": receivers}


async def _send_custom_notify_email(env, subject: str, text: str, html: str) -> dict:
    result = {
        "ok": True,
        "job": "custom-notify",
        "startedAt": _now_iso(),
        "finishedAt": _now_iso(),
        "summary": subject,
        "details": {
            "subject": subject,
            "textLength": len(text),
            "htmlLength": len(html),
        },
    }
    return await _send_job_report_email(env, result)


async def _trigger_e2e_runner(env) -> dict:
    webhook_url = _env_str(env, "E2E_RUNNER_WEBHOOK_URL", "")
    if not webhook_url:
        raise Exception("E2E_RUNNER_WEBHOOK_URL is not configured")

    headers = {"content-type": "application/json"}
    auth_header = _env_str(env, "E2E_RUNNER_AUTH_HEADER", "").strip()
    auth_token = _env_str(env, "E2E_RUNNER_AUTH_TOKEN", "").strip()
    if auth_header and auth_token:
        headers[auth_header] = auth_token

    payload = {
        "source": "zenos-jobs",
        "task": "weekly-e2e",
        "command": "./scripts/run-e2e.sh --ci",
        "requestedAt": _now_iso(),
        "timezone": "Asia/Kolkata",
        "schedule": "Thursday 00:00 IST",
    }

    response = await fetch(webhook_url, method="POST", headers=headers, body=json.dumps(payload))
    text = await response.text()

    try:
        parsed = json.loads(text)
    except Exception:
        parsed = {"raw": text}

    if not response.ok:
        raise Exception(f"runner webhook failed ({int(response.status)}): {text}")

    return parsed if isinstance(parsed, dict) else {"raw": parsed}


async def _run_weekly_e2e_job(env) -> dict:
    started_at = _now_iso()

    try:
        runner = await _trigger_e2e_runner(env)
        status = str(runner.get("status", "")).lower()
        ok = bool(runner.get("ok") is True or status in ["success", "passed"])

        summary = runner.get("summary")
        if not summary and isinstance(runner.get("metrics"), dict):
            m = runner.get("metrics", {})
            summary = (
                f"passed={m.get('passed', 0)}, failed={m.get('failed', 0)}, "
                f"skipped={m.get('skipped', 0)}, durationMinutes={m.get('durationMinutes', 0)}"
            )
        if not summary:
            summary = f"runner status: {runner.get('status', 'unknown')}"

    except Exception as error:
        runner = {"ok": False, "status": "failed", "summary": str(error)}
        ok = False
        summary = str(error)

    report_url = runner.get("reportUrl")
    run_id = runner.get("runId")
    report_base = _env_str(env, "E2E_REPORT_BASE_URL", "").rstrip("/")
    if (not report_url) and run_id and report_base:
        report_url = f"{report_base}/{run_id}"

    result = {
        "ok": ok,
        "job": "weekly-e2e",
        "startedAt": started_at,
        "finishedAt": _now_iso(),
        "summary": summary,
        "details": {
            "runId": run_id,
            "reportUrl": report_url,
            "status": runner.get("status"),
            "metrics": runner.get("metrics"),
            "runnerResponse": runner,
        },
    }

    email_result = await _send_job_report_email(env, result)
    result["details"]["email"] = email_result
    return result


def _resolve_cron_job(env, cron: str) -> dict | None:
    if cron == _env_str(env, "CACHE_WARM_CRON_CORE", "*/10 * * * *"):
        return {"job": "cache-warm", "service": "core"}
    if cron == _env_str(env, "CACHE_WARM_CRON_DISCOVERY", "*/30 * * * *"):
        return {"job": "cache-warm", "service": "discovery"}
    if cron == _env_str(env, "CACHE_WARM_CRON_SOCIAL", "15 * * * *"):
        return {"job": "cache-warm", "service": "social"}
    if cron == _env_str(env, "CACHE_WARM_CRON_ADMIN", "45 * * * *"):
        return {"job": "cache-warm", "service": "admin"}
    if cron == _env_str(env, "E2E_WEEKLY_CRON", "30 18 * * 3"):
        return {"job": "weekly-e2e"}
    return None


def _parse_service(raw: str | None) -> str:
    allowed = {"all", "core", "discovery", "social", "admin"}
    value = (raw or "all").strip().lower()
    return value if value in allowed else "all"


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(str(request.url))
        path = url.path

        if path == "/health":
            return _json_response(
                {
                    "ok": True,
                    "service": "zenos-jobs",
                    "runtime": "python",
                    "environment": _env_str(self.env, "ENVIRONMENT", "unknown"),
                }
            )

        if path == "/jobs/run":
            query = parse_qs(url.query)
            job = (query.get("job", ["cache-warm"])[0] or "cache-warm").strip()
            service = _parse_service(query.get("service", [None])[0])

            try:
                if job == "cache-warm":
                    result = await _run_cache_warm_job(self.env, service)
                elif job == "weekly-e2e":
                    result = await _run_weekly_e2e_job(self.env)
                else:
                    return _json_response({"ok": False, "error": f"Unknown job: {job}"}, status=400)

                return _json_response(result, status=200 if result.get("ok") else 500)
            except Exception as error:
                return _json_response({"ok": False, "error": str(error)}, status=500)

        if path == "/notify":
            if request.method != "POST":
                return _json_response({"ok": False, "error": "method_not_allowed"}, status=405)

            expected_secret = _env_str(self.env, "NOTIFY_SECRET", "")
            if not _has_valid_secret(request, expected_secret):
                return _json_response({"ok": False, "error": "unauthorized"}, status=401)

            try:
                payload = await request.json()
            except Exception:
                payload = {}

            subject = str(payload.get("subject", "")).strip()
            text = str(payload.get("text", "")).strip()
            html = str(payload.get("html", "")).strip()

            if not subject or not text or not html:
                return _json_response(
                    {"ok": False, "error": "subject, html, text are required"},
                    status=400,
                )

            try:
                email = await _send_custom_notify_email(self.env, subject, text, html)
                return _json_response({"ok": True, "email": email})
            except Exception as error:
                return _json_response({"ok": False, "error": str(error)}, status=500)

        return _json_response(
            {
                "ok": True,
                "service": "zenos-jobs",
                "runtime": "python",
                "endpoints": [
                    "/health",
                    "/jobs/run?job=cache-warm&service=core",
                    "/jobs/run?job=cache-warm&service=discovery",
                    "/jobs/run?job=cache-warm&service=social",
                    "/jobs/run?job=cache-warm&service=admin",
                    "/jobs/run?job=weekly-e2e",
                    "/notify",
                ],
            }
        )

    async def scheduled(self, controller, _ctx):
        cron = str(getattr(controller, "cron", ""))
        resolved = _resolve_cron_job(self.env, cron)
        if not resolved:
            print(f"No job mapped for cron: {cron}")
            return

        try:
            if resolved["job"] == "cache-warm":
                result = await _run_cache_warm_job(self.env, resolved.get("service", "all"))
            else:
                result = await _run_weekly_e2e_job(self.env)
            print(json.dumps(result))
        except Exception as error:
            print(json.dumps({"ok": False, "job": resolved["job"], "error": str(error)}))
