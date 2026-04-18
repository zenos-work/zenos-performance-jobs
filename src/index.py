import json
import re
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


async def _run_cache_warm_services(env, services: list[str]) -> dict:
    started_at = _now_iso()
    warmed = 0
    failed = 0
    results = []

    for service in services:
        result = await _run_cache_warm_job(env, service)
        details = result.get("details", {})
        warmed += int(details.get("warmed", 0))
        failed += int(details.get("failed", 0))
        results.append(result)

    finished_at = _now_iso()
    return {
        "ok": failed == 0,
        "job": "cache-warm",
        "startedAt": started_at,
        "finishedAt": finished_at,
        "summary": f"Warmed {warmed} URLs, failed {failed} (services={','.join(services)}).",
        "details": {
            "services": services,
            "warmed": warmed,
            "failed": failed,
            "results": results,
        },
    }


async def _run_cache_warm_core_discovery(env) -> dict:
    now = datetime.now(timezone.utc)
    services = ["core"]
    if now.minute % 30 == 0:
        services.append("discovery")
    return await _run_cache_warm_services(env, services)


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


# ── Notification delivery job ─────────────────────────────────────────────────
#
# Fetches FEATURE_ANNOUNCEMENT (and any other) notifications with
# delivery_status='pending' from the backend D1, sends them via the
# appropriate channel, then reports delivery status back.
#
# Email:  sent via Resend / webhook (same infra as job reports).
# Push:   sent as Web Push to push subscription endpoints.
#         Requires VAPID_PUBLIC_KEY + VAPID_PRIVATE_KEY_JWK env vars.
#         Falls back gracefully: push subscriptions without a configured
#         VAPID key-pair are marked 'failed' so they can be retried once
#         keys are provisioned.


def _build_notification_html(notif: dict) -> str:
    name = notif.get("user_name") or "Zenos user"
    message = notif.get("message") or ""
    ntype = (notif.get("type") or "").replace("_", " ").title()
    return (
        f"<div style='font-family:system-ui,sans-serif;max-width:600px;margin:0 auto'>"
        f"<h2 style='color:#c3a45c'>{ntype}</h2>"
        f"<p>Hi {name},</p>"
        f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:12px;border-radius:6px'>"
        f"{message}</pre>"
        f"<hr/><p style='color:#888;font-size:12px'>You received this because you are "
        f"enrolled in this feature tier. Manage preferences in your Zenos account.</p>"
        f"</div>"
    )


async def _send_notification_email(env, notif: dict) -> tuple[bool, str]:
    """Send one notification as email. Returns (ok, external_ref)."""
    user_email = (notif.get("user_email") or "").strip()
    if not user_email or "@" not in user_email:
        return False, "no-email"

    ntype = (notif.get("type") or "Notification").replace("_", " ").title()
    subject = f"[Zenos] {ntype}"
    text_body = notif.get("message") or ""
    html_body = _build_notification_html(notif)

    # Try Resend first, then webhook, matching existing job-report pattern.
    api_key = _env_str(env, "RESEND_API_KEY", "")
    if api_key:
        try:
            resp = await fetch(
                "https://api.resend.com/emails",
                method="POST",
                headers={
                    "authorization": f"Bearer {api_key}",
                    "content-type": "application/json",
                },
                body=json.dumps({
                    "from": _env_str(env, "RESEND_FROM", "noreply@zenos.work"),
                    "to": [user_email],
                    "subject": subject,
                    "text": text_body,
                    "html": html_body,
                }),
            )
            if resp.ok:
                try:
                    data = await resp.json()
                    ref = str(data.get("id") or "resend-ok")
                except Exception:
                    ref = "resend-ok"
                return True, ref
        except Exception:
            pass

    webhook_url = _env_str(env, "REPORT_EMAIL_WEBHOOK_URL", "")
    if webhook_url:
        try:
            headers = {"content-type": "application/json"}
            token = _env_str(env, "REPORT_EMAIL_WEBHOOK_TOKEN", "")
            if token:
                headers["authorization"] = f"Bearer {token}"
            resp = await fetch(
                webhook_url,
                method="POST",
                headers=headers,
                body=json.dumps({
                    "to": [user_email],
                    "subject": subject,
                    "text": text_body,
                    "html": html_body,
                }),
            )
            if resp.ok:
                return True, "webhook-ok"
        except Exception:
            pass

    return False, "no-provider"


def _build_vapid_jwt(
    endpoint: str,
    vapid_public: str,
    vapid_private_hex: str,
    subscriber: str = "mailto:ops@zenos.work",
) -> str | None:
    """
    Build a VAPID JWT (RFC 8292) using ES256.
    Requires the private key as a hex-encoded raw 32-byte scalar.
    Falls back to None if the 'cryptography' package is unavailable —
    the caller then skips VAPID and marks the notification as failed.
    The 'cryptography' package is available in Cloudflare Pyodide Workers
    at runtime; the linter may flag it as unresolved in local dev setups.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ec import (  # type: ignore[import]
            SECP256R1,
            derive_private_key,
        )
        from cryptography.hazmat.primitives.asymmetric.utils import (  # type: ignore[import]
            decode_dss_signature,
        )
        from cryptography.hazmat.backends import default_backend  # type: ignore[import]

        # Parse audience from endpoint origin.
        try:
            from urllib.parse import urlparse as _urlparse
            parsed = _urlparse(endpoint)
            audience = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return None

        # Build JWT header + payload (base64url-encoded, no padding).
        def _b64url(data: bytes) -> str:
            import base64
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

        import time
        now = int(time.time())
        header = _b64url(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
        payload = _b64url(json.dumps({
            "aud": audience,
            "exp": now + 43200,
            "sub": subscriber,
        }).encode())
        signing_input = f"{header}.{payload}".encode()

        # Sign with ECDSA P-256.
        private_int = int(vapid_private_hex, 16)
        private_key = derive_private_key(private_int, SECP256R1(), default_backend())
        from cryptography.hazmat.primitives import hashes  # type: ignore[import]
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA  # type: ignore[import]
        der_sig = private_key.sign(signing_input, ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_sig)
        sig_bytes = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        signature = _b64url(sig_bytes)

        return f"{header}.{payload}.{signature}"
    except Exception:
        return None


async def _send_notification_push(
    env, notif: dict, push_subs: list[dict]
) -> tuple[bool, str]:
    """
    Send a Web Push notification to all active subscriptions for one user.
    Returns (any_ok, comma-separated push delivery refs).
    """
    if not push_subs:
        return False, "no-subscriptions"

    vapid_public = _env_str(env, "VAPID_PUBLIC_KEY", "").strip()
    vapid_private_hex = _env_str(env, "VAPID_PRIVATE_KEY_HEX", "").strip()
    vapid_subscriber = _env_str(env, "VAPID_SUBSCRIBER", "mailto:ops@zenos.work").strip()

    push_payload = json.dumps({
        "title": (notif.get("type") or "Notification").replace("_", " ").title(),
        "body": (notif.get("message") or "")[:200],
        "tag": notif.get("group_key") or notif.get("id") or "",
        "url": "/notifications",
    }).encode("utf-8")

    any_ok = False
    refs = []
    for sub in push_subs:
        endpoint = (sub.get("endpoint") or "").strip()
        if not endpoint:
            continue
        try:
            headers: dict = {
                "content-type": "application/json",
                "content-encoding": "aesgcm",
                "ttl": "86400",
                "urgency": "normal",
            }

            if vapid_public and vapid_private_hex:
                jwt = _build_vapid_jwt(endpoint, vapid_public, vapid_private_hex, vapid_subscriber)
                if jwt:
                    headers["authorization"] = (
                        f"vapid t={jwt},k={vapid_public}"
                    )

            resp = await fetch(
                endpoint,
                method="POST",
                headers=headers,
                body=push_payload,
            )
            ok = bool(resp.status in (200, 201, 202, 204))
            if ok:
                any_ok = True
                refs.append(f"push:{resp.status}")
            else:
                refs.append(f"push:err:{resp.status}")
        except Exception as exc:
            refs.append(f"push:exc:{str(exc)[:40]}")

    return any_ok, ",".join(refs) if refs else "no-result"


async def _fetch_pending_from_backend(env, channel: str) -> list[dict]:
    """Call the backend internal endpoint to get pending notifications."""
    api_base = _env_str(env, "ZENOS_API_BASE_URL",
                        _env_str(env, "API_BASE_URL", "https://api.zenos.work"))
    service_secret = _env_str(env, "ZENOS_SERVICE_SECRET", "")
    if not service_secret:
        raise RuntimeError("ZENOS_SERVICE_SECRET is not configured")

    url = f"{api_base.rstrip('/')}/api/admin/notifications/pending-delivery?channel={channel}&limit=200"
    resp = await fetch(
        url,
        method="GET",
        headers={"authorization": f"Bearer {service_secret}"},
    )
    if not resp.ok:
        raise RuntimeError(f"pending-delivery fetch failed: {int(resp.status)}")
    data = await resp.json()
    return data.get("notifications", []) if isinstance(data, dict) else []


async def _report_delivery_to_backend(env, updates: list[dict]) -> None:
    """POST delivery statuses back to the backend."""
    if not updates:
        return
    api_base = _env_str(env, "ZENOS_API_BASE_URL",
                        _env_str(env, "API_BASE_URL", "https://api.zenos.work"))
    service_secret = _env_str(env, "ZENOS_SERVICE_SECRET", "")
    if not service_secret:
        return

    url = f"{api_base.rstrip('/')}/api/admin/notifications/delivery-status"
    await fetch(
        url,
        method="POST",
        headers={
            "authorization": f"Bearer {service_secret}",
            "content-type": "application/json",
        },
        body=json.dumps({"updates": updates}),
    )


async def _run_notification_delivery_job(env) -> dict:
    started_at = _now_iso()
    results: dict = {
        "email": {"sent": 0, "failed": 0, "skipped": 0},
        "push": {"sent": 0, "failed": 0, "skipped": 0},
    }
    errors: list[str] = []

    # ── Email channel ────────────────────────────────────────────────────────
    try:
        email_notifications = await _fetch_pending_from_backend(env, "email")
        email_updates = []
        for notif in email_notifications:
            try:
                ok, ref = await _send_notification_email(env, notif)
                status = "delivered" if ok else "failed"
                if ok:
                    results["email"]["sent"] += 1
                else:
                    results["email"]["failed"] += 1
                email_updates.append(
                    {"id": notif["id"], "status": status, "external_ref": ref}
                )
            except Exception as exc:
                results["email"]["failed"] += 1
                email_updates.append(
                    {"id": notif["id"], "status": "failed", "external_ref": str(exc)[:80]}
                )
        await _report_delivery_to_backend(env, email_updates)
    except Exception as exc:
        errors.append(f"email: {exc}")

    # ── Push channel ─────────────────────────────────────────────────────────
    try:
        push_notifications = await _fetch_pending_from_backend(env, "push")
        push_updates = []
        for notif in push_notifications:
            subs = notif.get("push_subscriptions") or []
            if not subs:
                results["push"]["skipped"] += 1
                push_updates.append(
                    {"id": notif["id"], "status": "failed", "external_ref": "no-subscriptions"}
                )
                continue
            try:
                ok, ref = await _send_notification_push(env, notif, subs)
                status = "delivered" if ok else "failed"
                if ok:
                    results["push"]["sent"] += 1
                else:
                    results["push"]["failed"] += 1
                push_updates.append(
                    {"id": notif["id"], "status": status, "external_ref": ref}
                )
            except Exception as exc:
                results["push"]["failed"] += 1
                push_updates.append(
                    {"id": notif["id"], "status": "failed", "external_ref": str(exc)[:80]}
                )
        await _report_delivery_to_backend(env, push_updates)
    except Exception as exc:
        errors.append(f"push: {exc}")

    finished_at = _now_iso()
    ok = len(errors) == 0
    return {
        "ok": ok,
        "job": "notification-delivery",
        "startedAt": started_at,
        "finishedAt": finished_at,
        "summary": (
            f"Email: sent={results['email']['sent']} failed={results['email']['failed']}  "
            f"Push: sent={results['push']['sent']} failed={results['push']['failed']} "
            f"skipped={results['push']['skipped']}"
        ),
        "details": {
            "results": results,
            "errors": errors,
        },
    }


async def _run_platform_snapshot_job(env, feature: str = "all") -> dict:
    started_at = _now_iso()
    allowed = {"all", "courses", "community", "marketplace", "connectors"}
    selected = feature if feature in allowed else "all"

    checks = {
        "courses": {
            "url": "/api/courses?limit=5",
            "metric": "courses",
        },
        "community": {
            "url": "/api/community?limit=5",
            "metric": "spaces",
        },
        "marketplace": {
            "url": "/api/marketplace?limit=5",
            "metric": "items",
        },
        "connectors": {
            "url": "/api/connector-marketplace",
            "metric": "listings",
        },
    }

    api_base = _env_str(env, "API_BASE_URL", "https://api.zenos.work").rstrip("/")
    service_secret = _env_str(env, "ZENOS_SERVICE_SECRET", "")
    headers = {}
    if service_secret:
        headers["authorization"] = f"Bearer {service_secret}"

    targets = checks.keys() if selected == "all" else [selected]
    details = {}
    failures = 0

    for key in targets:
        config = checks[key]
        url = f"{api_base}{config['url']}"
        try:
            response = await fetch(url, method="GET", headers=headers)
            ok = bool(response.ok)
            parsed = {}
            try:
                parsed = await response.json()
            except Exception:
                parsed = {}
            metric_key = config["metric"]
            metric_value = parsed.get(metric_key)
            metric_count = len(metric_value) if isinstance(metric_value, list) else 0
            details[key] = {
                "ok": ok,
                "status": int(response.status),
                "metric": metric_key,
                "count": metric_count,
            }
            if not ok:
                failures += 1
        except Exception as error:
            failures += 1
            details[key] = {"ok": False, "error": str(error)}

    return {
        "ok": failures == 0,
        "job": "platform-snapshot",
        "startedAt": started_at,
        "finishedAt": _now_iso(),
        "summary": f"Snapshot completed for {selected}; failures={failures}.",
        "details": {
            "feature": selected,
            "checks": details,
            "failures": failures,
        },
    }


def _resolve_cron_job(env, cron: str) -> dict | None:
    if cron == _env_str(env, "CACHE_WARM_CRON_CORE", "*/10 * * * *"):
        return {"job": "cache-warm", "service": "core-discovery"}
    if cron == _env_str(env, "CACHE_WARM_CRON_SOCIAL_ADMIN", "15,45 * * * *"):
        return {"job": "cache-warm", "service": "social-admin"}
    if cron == _env_str(env, "E2E_WEEKLY_CRON", "30 18 * * 3"):
        return {"job": "weekly-e2e"}
    if cron == _env_str(env, "NOTIFICATION_DELIVERY_CRON", "*/5 * * * *"):
        return {"job": "notification-delivery"}
    if cron == _env_str(env, "PLATFORM_SNAPSHOT_CRON", "15 */6 * * *"):
        return {"job": "platform-snapshot", "feature": "all"}
    return None


def _parse_service(raw: str | None) -> str:
    allowed = {"all", "core", "discovery", "social", "admin", "core-discovery", "social-admin"}
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
            feature = (query.get("feature", ["all"])[0] or "all").strip().lower()

            try:
                if job == "cache-warm":
                    if service == "core-discovery":
                        result = await _run_cache_warm_core_discovery(self.env)
                    elif service == "social-admin":
                        result = await _run_cache_warm_services(self.env, ["social", "admin"])
                    else:
                        result = await _run_cache_warm_job(self.env, service)
                elif job == "weekly-e2e":
                    result = await _run_weekly_e2e_job(self.env)
                elif job == "notification-delivery":
                    result = await _run_notification_delivery_job(self.env)
                elif job == "platform-snapshot":
                    result = await _run_platform_snapshot_job(self.env, feature)
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
                    "/jobs/run?job=cache-warm&service=core-discovery",
                    "/jobs/run?job=cache-warm&service=social-admin",
                    "/jobs/run?job=weekly-e2e",
                    "/jobs/run?job=notification-delivery",
                    "/jobs/run?job=platform-snapshot&feature=all",
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
                service = resolved.get("service", "all")
                if service == "core-discovery":
                    result = await _run_cache_warm_core_discovery(self.env)
                elif service == "social-admin":
                    result = await _run_cache_warm_services(self.env, ["social", "admin"])
                else:
                    result = await _run_cache_warm_job(self.env, service)
            elif resolved["job"] == "notification-delivery":
                result = await _run_notification_delivery_job(self.env)
            elif resolved["job"] == "platform-snapshot":
                result = await _run_platform_snapshot_job(self.env, resolved.get("feature", "all"))
            else:
                result = await _run_weekly_e2e_job(self.env)
            print(json.dumps(result))
        except Exception as error:
            print(json.dumps({"ok": False, "job": resolved["job"], "error": str(error)}))
