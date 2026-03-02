# Agentic Dev System (MVP)

Webhook-driven agent workflow for GitHub issues and PR comments with mandatory human approval before execution.

## What it does

- Receives GitHub webhook events.
- Queues candidate work items.
- Waits for explicit approval.
- Runs one agent task at a time.
- Creates branch, commit, push, and PR.

## Repository layout

- Service repo: this project (`pastoriomarco/agentic-dev-system`).
- Target repo: any repo that sends webhooks and is accessible with `GITHUB_TOKEN`.

The service clones target repos into `workspace/` inside the container at execution time.

## Quick setup (your current path)

```bash
cd /home/tndlux/workspaces/isaac_ros-dev/src/agentic-dev-system
cp .env.example .env
```

Edit `.env` with real values:

- `GITHUB_WEBHOOK_SECRET`
- `DEPLOYMENT_ENV` (`production` enforces non-empty `GITHUB_WEBHOOK_SECRET`)
- `WEBHOOK_DELIVERY_TTL_SECONDS` (Redis dedup retention for `X-GitHub-Delivery`)
- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`
- `ADMIN_API_TOKEN` (recommended)
- `GITHUB_STATUS_*` and `GH_LABEL_*` for default GitHub-side ownership signaling
- `REDIS_URL` (defaults to local compose Redis)
- `ALLOWED_REPOS` (recommended for multi-repo safety)
- `MAX_RETRIES`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `RETRY_POLL_INTERVAL_SECONDS`
- `WORKER_*` limits (CPU/memory/pids/timeout/image/network)
- `AGENT_*` controls for changed-file policy, diff policy, and quality gates
- `AGENT_PERMISSIONS_FILE` for explicit runtime permission context

Start:

```bash
docker-compose up -d --build
```

## GitHub webhook setup

In target repository settings:

- Payload URL: `https://<public-host>/webhook/github`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET`
- Events:
- `issues`
- `pull_request`
- `issue_comment`
- `pull_request_review`
- `pull_request_review_comment`
- Supported actions are explicitly filtered (for example: `issues.opened`, `pull_request.opened/synchronize`, `issue_comment.created`).

## API usage

Health:

```bash
curl http://localhost:8000/health
```

Deep health (dependencies + proxy path):

```bash
curl http://localhost:8000/health/deep
```

List queue:

```bash
curl http://localhost:8000/api/issues
```

Approve (starts agent):

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

Dead-letter list:

```bash
curl http://localhost:8000/api/dead-letter/issues
```

Requeue dead-lettered issue:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/issues/<queue_key>/requeue
```

Retry/dead-letter integration test:

```bash
docker build -t agentic-dev-system:latest .
docker run --rm agentic-dev-system:latest python -m unittest discover -s tests -v
```

List sessions:

```bash
curl http://localhost:8000/api/sessions
```

Queue key format:

- `owner:repo:issue_number`
- Example: `pastoriomarco:agentic-dev-system:12`

## Operational model

- `AGENT.md` defines agent behavior and quality/safety rules.
- Only approved items are executed.
- Execution is serialized through an internal lock (one task at a time).
- Queue and session persistence are stored in Redis.
- Webhooks are accepted only from allowlisted repos.
: If `ALLOWED_REPOS` is empty, the service defaults to allowlisting `GITHUB_OWNER/GITHUB_REPO` when set.
- In `production`, startup fails if `GITHUB_WEBHOOK_SECRET` is empty; unsigned webhooks are rejected.
- Webhook delivery IDs (`X-GitHub-Delivery`) are deduplicated in Redis to prevent replay/duplicate processing.
- Failed runs are retried with exponential backoff and moved to dead-letter after max retries.
- Each approved issue runs inside a short-lived worker container with:
: read-only root filesystem, dropped Linux caps, no-new-privileges, CPU/memory/pids limits, dedicated per-task workspace volume.
- Worker containers are destroyed after execution; workspace volume is removed; artifacts/logs are retained.
- The orchestrator now performs repo-aware edits through LLM-generated file operations.
- Commit is blocked unless quality gates pass and policy checks pass (changed file count, diff size, forbidden paths).
- Agent permissions are explicitly provided from `AGENT_PERMISSIONS.md` and injected into LLM system prompts.
- GitHub issue/PR status is signaled automatically via labels and comments (default labels):
: `agent:queued`, `agent:in-progress`, `agent:pr-opened`, `agent:failed`, `agent:dead-letter`, `agent:rejected`

## Practical deployment suggestions

- Run this service once (not once per target repo).
- Use one dedicated bot token or GitHub App installation per org/project.
- Keep this service on a stable host and expose `/webhook/github` using a reverse proxy.
- If testing locally, tunnel with `cloudflared` or `ngrok`.
- Restrict approval API access at network layer and with `ADMIN_API_TOKEN`.
- Set `ALLOWED_REPOS` explicitly in production.
: Example: `ALLOWED_REPOS=pastoriomarco/agentic-dev-system,pastoriomarco/private-repo`
- Set `DEPLOYMENT_ENV=production` in deployed environments so signature checks fail closed on misconfiguration.
- This design requires Docker socket access by the webhook service.
: Treat that host as privileged infrastructure and isolate it from untrusted multi-tenant workloads.
- Worker internet egress is restricted through Squid proxy allowlist (`proxy/squid.conf`), defaulting to GitHub domains.
: To allow extra destinations (for example remote LLM APIs), update `proxy/squid.conf` explicitly.
- Use `/health/deep` for preflight checks (Redis, Docker daemon, proxy->GitHub, optional LLM endpoint).
- Optional auto-assignment is available (`GITHUB_ASSIGN_ON_PROCESSING=true` with `GITHUB_ASSIGNEE_LOGIN=<bot-user>`).

## Current limitations

- LLM-generated edits can still fail on complex repos or ambiguous requirements; add repository-specific prompts/rules to improve reliability.
- Domain-level allowlisting cannot guarantee single-repo access by itself; enforce single-repo scope with GitHub App/fine-grained token permissions.

## Next hardening steps

See [FUTURE_IMPROVEMENTS.md](./FUTURE_IMPROVEMENTS.md) for the re-evaluated, prioritized roadmap (highest to lowest) with impact and complexity estimates.
