# Agentic Dev System User Manual

This manual is the operational reference for running and maintaining the service in local and production-like environments.

Use this together with:

- `README.md` for fast onboarding
- `FUTURE_IMPROVEMENTS.md` for roadmap and hardening backlog
- `AGENT.md` and `AGENT_PERMISSIONS.md` for agent behavior/policy context

## 1. System Overview

The service receives GitHub issue webhooks plus a narrow set of pull request events, stores candidate work in Redis, and executes approved items in isolated worker containers.

Core components:

- Webhook/API service (`webhook_handler.py`)
- Redis (queue/session state)
- Worker execution container (`worker_entrypoint.py` + `agent_orchestrator.py`)
- Squid egress proxy (`proxy/squid.conf`)

High-level flow:

1. GitHub sends webhook to `/webhook/github`.
2. Service verifies signature and basic ingress controls.
3. Service stores queue item with status `queued`.
4. Human approves via `/api/issues/{queue_key}/approve` for issue tasks or `/api/tasks/{task_id}/approve` for PR tasks.
5. Service starts worker container and tracks session.
6. Item moves to `completed`, `queued_retry`, `needs_human`, or `dead_letter`.

## 2. Prerequisites

- Docker Engine + Docker Compose
- Network path for GitHub webhooks to reach this service
- GitHub token with required repo permissions
- GitHub webhook secret configured both in GitHub and `.env`

Recommended:

- Dedicated bot identity/token
- Stable host (not developer laptop) for long-running deployments
- Network-level protection for approval APIs

## 3. Quick Start

From repository root:

```bash
cd /path/to/agentic-dev-system
cp .env.example .env
```

Set required values in `.env`:

- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`
- `GITHUB_OWNER` and `GITHUB_REPO` (or explicit `ALLOWED_REPOS`)
- `ADMIN_API_TOKEN` (required in production)

For a first local test, use a throwaway repository and a dedicated low-scope token. Keep `DEPLOYMENT_ENV=development` unless you are explicitly validating production startup behavior.

If you do not yet have a reachable LLM endpoint, you can still validate container build, health checks, webhook ingress, queueing, and approval API behavior. Do not approve tasks for execution until `LLM_API_URL` points to a real reachable endpoint.

Start stack:

```bash
docker-compose up -d --build
```

Basic checks:

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/deep
```

Recommended local-first `.env` profile:

- Set `ALLOWED_REPOS=<your-user>/<your-throwaway-repo>` explicitly.
- Set `GITHUB_STATUS_LABELS_ENABLED=false` and `GITHUB_STATUS_COMMENTS_ENABLED=false` if you want to validate queueing/API behavior before writing labels or comments back to GitHub.
- Leave `DEEP_HEALTH_CHECK_LLM=false` unless you intentionally want startup health to probe the LLM endpoint.
- If you are using a host-local LLM via `host.docker.internal`, keep `WORKER_ENABLE_HOST_GATEWAY=true`, `LLM_HOST_ALLOWLIST=host.docker.internal`, and include `host.docker.internal` in `WORKER_NO_PROXY`.

## 4. Configuration Reference

### 4.1 Webhook and runtime safety

- `GITHUB_WEBHOOK_SECRET`: GitHub HMAC secret.
- `DEPLOYMENT_ENV`: runtime mode, `development` or `production`.
- `WEBHOOK_DELIVERY_TTL_SECONDS`: dedup retention for `X-GitHub-Delivery` (default `86400`).
- `WEBHOOK_MAX_BODY_BYTES`: hard cap for accepted webhook bodies (default `262144`).
- `WEBHOOK_RATE_LIMIT_WINDOW_SECONDS`: fixed-window length for `/webhook/github` throttling (default `60`).
- `WEBHOOK_RATE_LIMIT_GLOBAL_MAX`: max accepted webhook requests per window across all repos (default `120`).
- `WEBHOOK_RATE_LIMIT_REPO_MAX`: max accepted webhook requests per window per repo (default `60`).

Important behavior:

- If `DEPLOYMENT_ENV=production` and `GITHUB_WEBHOOK_SECRET` is empty, startup fails.
- If `DEPLOYMENT_ENV=production` and `ADMIN_API_TOKEN` is empty, startup fails.
- Supported webhook deliveries require `X-GitHub-Delivery`.
- Duplicate delivery IDs inside TTL are ignored.
- Oversize webhook bodies return `413 payload_too_large`.
- Fixed-window ingress throttling returns `429 rate_limited` with `Retry-After`.

### 4.2 Repo scoping and access

- `GITHUB_OWNER`, `GITHUB_REPO`: fallback single-repo allowlist if `ALLOWED_REPOS` is empty.
- `ALLOWED_REPOS`: comma-separated `owner/repo` allowlist.
- `GITHUB_TOKEN`: token used for GitHub API operations and clone/push. The worker only exposes write credentials during publish/comment steps; quality gates run without write-token subprocess environment.
- `LLM_API_URL`: worker LLM endpoint URL.
- `LLM_HOST_ALLOWLIST`: comma-separated exact hosts or dot-prefixed suffixes allowed for `LLM_API_URL`.

Allowlist behavior:

- If `ALLOWED_REPOS` is set: only listed repos are accepted.
- If `ALLOWED_REPOS` is empty and `GITHUB_OWNER/GITHUB_REPO` are set: only that repo is accepted.
- If none are set: all repos are accepted (not recommended).

### 4.3 Approval/API protection

- `ADMIN_API_TOKEN`: protects approval/reject/requeue endpoints using `X-Admin-Token`.

If unset, admin endpoints are open to any caller reaching the service in development. Production startup fails if it is empty.

### 4.4 Retry behavior

- `MAX_RETRIES`
- `RETRY_BASE_DELAY_SECONDS`
- `RETRY_MAX_DELAY_SECONDS`
- `RETRY_POLL_INTERVAL_SECONDS`

Backoff is exponential and capped by `RETRY_MAX_DELAY_SECONDS`.

### 4.5 Worker execution controls

- `WORKER_IMAGE`
- `WORKER_NETWORK`
- `WORKER_TIMEOUT_SECONDS`
- `WORKER_CPU_LIMIT`
- `WORKER_MEMORY_LIMIT`
- `WORKER_PIDS_LIMIT`
- `WORKER_RUN_AS_UID`
- `WORKER_RUN_AS_GID`
- `WORKER_ENABLE_HOST_GATEWAY`
- `WORKER_ARTIFACTS_DIR`
- `WORKER_ARTIFACTS_VOLUME`
- `WORKER_VOLUME_PREFIX`

Current execution model uses Docker socket access from webhook service. Treat host as privileged infrastructure.
Worker containers now run as the configured non-root UID/GID by default; a short-lived root-owned prep container fixes volume permissions before the real worker starts.

### 4.6 Egress controls

- `WORKER_HTTP_PROXY`
- `WORKER_HTTPS_PROXY`
- `WORKER_NO_PROXY`
- Squid allowlist in `proxy/squid.conf`

Default Squid config allows GitHub domains only.

Important behavior:

- Public `LLM_API_URL` hosts must be in `LLM_HOST_ALLOWLIST`, must not appear in `WORKER_NO_PROXY`, and must be present in Squid `allowed_domains`.
- Private or loopback `LLM_API_URL` hosts must be in `LLM_HOST_ALLOWLIST` and must appear in `WORKER_NO_PROXY`.
- Metadata and link-local targets are blocked.
- `host.docker.internal` access inside worker containers is opt-in via `WORKER_ENABLE_HOST_GATEWAY=true`.
- Compose still maps `host.docker.internal` into the webhook container for operator-side local LLM/deep-health use.

### 4.7 Agent quality and policy controls

- `AGENT_MAX_CHANGED_FILES`
- `AGENT_MAX_DIFF_LINES`
- `AGENT_MAX_EDIT_ACTIONS`
- `AGENT_ALLOWED_PATH_PREFIXES`
- `AGENT_FORBIDDEN_PATH_PREFIXES`
- `AGENT_QUALITY_COMMANDS`
- `AGENT_QUALITY_TIMEOUT_SECONDS`
- `AGENT_ALLOW_NO_QUALITY_GATES`
- `AGENT_PERMISSIONS_FILE`

Important behavior:

- LLM plan responses must include a non-empty `summary`.
- LLM edit responses may contain only `summary` and `edits`.
- Invalid or policy-violating LLM output is escalated to `needs_human` before any file edit is applied.
- LLM endpoint network policy is enforced both at startup and inside the worker before outbound LLM requests.
- The stock worker image includes `pytest` and `ruff`, so the default `.env.example` value for `AGENT_QUALITY_COMMANDS` works without extra image changes.

## 5. GitHub Webhook Setup

Repository settings:

- Payload URL: `https://<host>/webhook/github`
- Content type: `application/json`
- Secret: same as `GITHUB_WEBHOOK_SECRET`

Subscribe to events:

- `issues`
- `issue_comment`
- `pull_request`
- `pull_request_review`
- `pull_request_review_comment`

Supported actions:

- `issues`: `opened`, `edited`, `reopened`
- `issue_comment`: `created`
- `pull_request`: `synchronize`
- `pull_request_review`: `submitted`
- `pull_request_review_comment`: `created`

Important limitations:

- `issue_comment` on an issue thread is always eligible for queueing under the normal trigger rules.
- `issue_comment` on a pull request creates a task only when the comment contains `@agent` or `@ai`.
- `pull_request_review_comment` creates a task only when the review comment contains `@agent` or `@ai` and includes file/line context.
- `pull_request_review` with action `submitted` creates a task only when the review body contains `@agent` or `@ai`.
- Pull request tasks require complete head/base metadata from GitHub before they can be queued safely.
- Forked pull request tasks are supported only through helper PR publish mode in the base repo; the agent never pushes directly to contributor branches.
- Review-comment tasks are restricted to the commented file even if the model proposes a broader edit.
- `pull_request.synchronize` does not create a new task; it moves queued/approved/open PR tasks for the old head SHA to `needs_human`.

Unsupported events/actions are accepted at HTTP level but ignored with status `202`.
Ingress limits apply before task creation, so `413` and `429` responses do not enqueue or deduplicate work items.

## 6. API Runbook

### 6.1 Health

```bash
curl http://localhost:8000/health
curl http://localhost:8000/health/deep
```

`/health/deep` validates Redis, Docker daemon, and proxy path.

### 6.2 Queue and issue state

```bash
curl http://localhost:8000/api/issues
curl http://localhost:8000/api/issues/<queue_key>
curl http://localhost:8000/api/dead-letter/issues
curl http://localhost:8000/api/sessions
curl http://localhost:8000/api/tasks
curl http://localhost:8000/api/tasks/<task_id>
```

Queue key format:

- `owner:repo:issue_number`
- Example: `pastoriomarco:agentic-dev-system:42`
- GitHub issue and pull request numbers share one namespace, so the queue projection exposes `subject_kind` to distinguish them.

### 6.3 Manual approvals

Approve:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/issues/<queue_key>/approve
```

Reject:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/issues/<queue_key>/reject
```

Requeue dead-letter item:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/issues/<queue_key>/requeue
```

If `ADMIN_API_TOKEN` is not set, omit `X-Admin-Token`.

Pull request task approvals must use the task-level endpoints:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/tasks/<task_id>/approve

curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/tasks/<task_id>/reject

curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/tasks/<task_id>/requeue
```

## 7. State and Label Model

Main queue statuses:

- `queued`
- `approved`
- `processing`
- `completed`
- `queued_retry`
- `needs_human`
- `dead_letter`
- `rejected`

Default GitHub labels:

- `agent:queued`
- `agent:in-progress`
- `agent:needs-human`
- `agent:pr-opened`
- `agent:failed`
- `agent:dead-letter`
- `agent:rejected`

The service can also post issue comments for key state changes.

For supported PR tasks, `needs_human` is also used when:

- the service restarts while a PR task is `processing`,
- the PR head SHA changes before publish,
- the agent attempts to edit files outside the PR's current changed-file set.

## 8. Ingress Behavior and Responses

Webhook endpoint: `POST /webhook/github`

Common outcomes:

- `401 Invalid signature`: missing/invalid `X-Hub-Signature-256`.
- `413 payload_too_large`: request exceeds `WEBHOOK_MAX_BODY_BYTES`.
- `429 rate_limited`: request exceeded global or per-repo fixed-window ingress limits.
- `400 Missing X-GitHub-Delivery header`: required for supported events/actions.
- `202 ignored repo_not_allowlisted`
- `202 ignored unsupported_event`
- `202 ignored unsupported_action`
- `202 ignored pull_request_comment_without_agent_trigger`
- `202 ignored pull_request_review_comment_without_agent_trigger`
- `202 ignored pull_request_review_without_agent_trigger`
- `202 ignored pull_request_review_comment_context_incomplete`
- `202 ignored pull_request_context_incomplete`
- `202 ignored duplicate_delivery`
- `200 received`: accepted and queued for background processing, or a `pull_request.synchronize` stale-task reconciliation was applied.

## 9. Operations and Monitoring

Recommended routine:

1. Check `/health/deep`.
2. Review queue with `/api/issues`.
3. Inspect `/api/tasks` when a queue projection represents a pull request task.
4. Approve or reject pending items.
5. Check `/api/sessions` for run outcomes.
6. Check `/api/dead-letter/issues` and requeue if appropriate.

If using compose logs:

```bash
docker-compose logs -f webhook
docker-compose logs -f redis
docker-compose logs -f egress-proxy
```

## 10. Troubleshooting

### 10.1 Webhooks not queued

Check:

- GitHub webhook delivery log status code and response body.
- `GITHUB_WEBHOOK_SECRET` matches GitHub secret.
- `DEPLOYMENT_ENV=production` is not combined with empty secret.
- `WEBHOOK_MAX_BODY_BYTES` is large enough for expected GitHub payload sizes.
- `WEBHOOK_RATE_LIMIT_*` is sized for expected webhook bursts.
- `LLM_HOST_ALLOWLIST` contains the configured LLM hostname.
- Public LLM hosts are not listed in `WORKER_NO_PROXY`; private/local LLM hosts are.
- If `LLM_API_URL` uses `host.docker.internal`, `WORKER_ENABLE_HOST_GATEWAY=true` is set.
- `ALLOWED_REPOS` and repository name formatting (`owner/repo`).
- Event type/action is in supported matrix.
- Duplicate delivery IDs are not being replayed.
- Pull request issue comments include `@agent` or `@ai`.
- Pull request still has valid head/base metadata; fork PRs are supported only through helper-PR publish mode.

### 10.2 Approval API returns 401

Check:

- `ADMIN_API_TOKEN` value in `.env`.
- Exact header name: `X-Admin-Token`.
- Reverse proxy does not strip custom headers.

### 10.3 Runs move to `queued_retry` or `dead_letter`

Check:

- `api/sessions` errors for failed run details.
- Worker image availability and startup.
- Git credentials, repo permissions, and branch protections.
- Quality command failures and timeout (`AGENT_QUALITY_*`).
- Whether malformed LLM JSON or unsupported edit fields moved the task to `needs_human` instead of retrying.

### 10.4 Runs move to `needs_human`

Check:

- Whether the service restarted while a task was `processing` and no completed worker artifact was available for automatic re-ingestion.
- Whether an operator attempted an invalid state transition.
- Whether a pull request head SHA moved or the worker blocked an out-of-scope edit.
- Whether the task should be manually reapproved with `/requeue` or replaced by a newer event/task.

### 10.5 Deep health degraded

Check:

- Redis connectivity from webhook container.
- Docker daemon/socket accessibility.
- Proxy egress to GitHub (`DEEP_HEALTH_GITHUB_URL`).
- Optional LLM endpoint availability if enabled in deep health.
- Squid `allowed_domains`, `LLM_HOST_ALLOWLIST`, and `WORKER_NO_PROXY` agree on whether the LLM route is proxied or direct.
- `WORKER_ENABLE_HOST_GATEWAY` is enabled only when the worker must reach `host.docker.internal`.

## 11. Security Hardening Checklist

Use this as minimum baseline:

1. Set `DEPLOYMENT_ENV=production`.
2. Set strong `GITHUB_WEBHOOK_SECRET`.
3. Set `ADMIN_API_TOKEN` and restrict admin API network access.
4. Set explicit `ALLOWED_REPOS`.
5. Keep Squid allowlist minimal.
6. Keep `WORKER_ENABLE_HOST_GATEWAY=false` unless a host-local LLM route is explicitly required.
7. Isolate host with Docker socket exposure.
8. Monitor dead-letter queue and repeated failures.

## 12. Testing and Validation

### 12.1 Containerized regression tests

Run the bundled test suite in the same containerized environment used by the service:

```bash
docker build -t agentic-dev-system:latest .
docker run --rm agentic-dev-system:latest python -m unittest discover -s tests -v
```

### 12.2 Local stack smoke test

This validates build/startup, ingress handling, queue state, and admin APIs before you try a real agent run.

1. Copy `.env.example` to `.env`.
2. Set `GITHUB_WEBHOOK_SECRET`, `ADMIN_API_TOKEN`, and explicit `ALLOWED_REPOS`.
3. If you are only smoke-testing queueing and APIs, it is fine to leave `DEEP_HEALTH_CHECK_LLM=false` and skip task approvals.
4. Start the stack with `docker-compose up -d --build`.
5. Verify `curl http://localhost:8000/health` and `curl http://localhost:8000/health/deep`.
6. Post a signed local webhook directly to the service:

```bash
PAYLOAD='{"action":"opened","issue":{"number":101,"title":"Local test issue","body":"Do not approve unless repo+LLM are configured."},"repository":{"full_name":"<owner>/<repo>","clone_url":"https://github.com/<owner>/<repo>.git"},"sender":{"login":"local-tester"}}'
SIG=$(printf %s "$PAYLOAD" | openssl dgst -sha256 -hmac "$GITHUB_WEBHOOK_SECRET" | sed 's/^.* //')

curl -X POST http://localhost:8000/webhook/github \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: issues" \
  -H "X-GitHub-Delivery: local-test-issue-101" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -d "$PAYLOAD"
```

7. Confirm the task appears in `curl http://localhost:8000/api/issues`.
8. Confirm `curl http://localhost:8000/api/tasks` contains the immutable task record.
9. If you do not yet have a real reachable `LLM_API_URL`, stop here. The ingress, queueing, and approval surface are now validated without risking a failed worker run.

### 12.3 End-to-end throwaway-repo test

Use this only after the smoke test passes.

1. Use a throwaway repository that your `GITHUB_TOKEN` can clone, push to, and open PRs against.
2. Set `GITHUB_OWNER`, `GITHUB_REPO`, or `ALLOWED_REPOS` to that repo only.
3. Configure a real reachable `LLM_API_URL`.
4. If the LLM is host-local, use `host.docker.internal`, keep `WORKER_ENABLE_HOST_GATEWAY=true`, and ensure `LLM_HOST_ALLOWLIST` and `WORKER_NO_PROXY` match that route.
5. Keep the default `AGENT_QUALITY_COMMANDS` unless the target repo needs a different quality gate.
6. Start the stack and confirm `/health/deep` is healthy enough for your intended route.
7. Create a real issue in the throwaway repo or deliver a signed local `issues.opened` webhook that points at that repo.
8. Approve the queued task with `/api/issues/<queue_key>/approve`.
9. Watch `docker-compose logs -f webhook` and inspect `/api/sessions`.
10. Confirm the run either opens a PR or lands in `needs_human` with an actionable reason.

### 12.4 Webhook ingress/manual behavior checks

Validate webhook ingress controls manually:

1. Send signed webhook with unique `X-GitHub-Delivery`.
2. Replay same payload with same delivery ID and expect dedup ignore.
3. Send an oversize payload and confirm `413 payload_too_large`.
4. Burst requests above the configured limit and confirm `429 rate_limited`.
5. Send unsupported action and confirm ignore.
6. Send a PR issue comment without `@agent` and confirm ignore.
7. Send a same-repo PR issue comment with `@agent` and confirm a pull request task appears in `/api/tasks`.
8. Send a same-repo PR review comment with `@agent` and confirm the task contains `comment.path` and line metadata.
9. Send a submitted PR review body with `@agent` and confirm a pull request task appears in `/api/tasks`.
10. Send `pull_request.synchronize` for that PR and confirm open tasks move to `needs_human`.
11. Remove signature and confirm rejection.

## 13. Documentation Conventions

Documentation split is intentional:

- `README.md`: fast path and orientation.
- `docs/USER_MANUAL.md`: detailed operating guide.
- `FUTURE_IMPROVEMENTS.md`: roadmap and prioritization.
