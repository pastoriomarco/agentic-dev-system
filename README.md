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
- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`
- `ADMIN_API_TOKEN` (recommended)
- `REDIS_URL` (defaults to local compose Redis)
- `ALLOWED_REPOS` (recommended for multi-repo safety)
- `MAX_RETRIES`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `RETRY_POLL_INTERVAL_SECONDS`
- `WORKER_*` limits (CPU/memory/pids/timeout/image/network)

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

## API usage

Health:

```bash
curl http://localhost:8000/health
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
- Failed runs are retried with exponential backoff and moved to dead-letter after max retries.
- Each approved issue runs inside a short-lived worker container with:
: read-only root filesystem, dropped Linux caps, no-new-privileges, CPU/memory/pids limits, dedicated per-task workspace volume.
- Worker containers are destroyed after execution; workspace volume is removed; artifacts/logs are retained.

## Practical deployment suggestions

- Run this service once (not once per target repo).
- Use one dedicated bot token or GitHub App installation per org/project.
- Keep this service on a stable host and expose `/webhook/github` using a reverse proxy.
- If testing locally, tunnel with `cloudflared` or `ngrok`.
- Restrict approval API access at network layer and with `ADMIN_API_TOKEN`.
- Set `ALLOWED_REPOS` explicitly in production.
: Example: `ALLOWED_REPOS=pastoriomarco/agentic-dev-system,pastoriomarco/private-repo`
- This design requires Docker socket access by the webhook service.
: Treat that host as privileged infrastructure and isolate it from untrusted multi-tenant workloads.

## Current limitations

- Code generation is currently deterministic scaffolding in `agentic_changes/issue_<n>.md`.
- Worker networking is still broad (`bridge`) by default; tighten egress with host firewall/network policies if needed.

## Next hardening steps

- Add durable SQL state for audit/reporting (Postgres) alongside Redis operational state.
- Add policy checks before PR creation.
- Add path-level restrictions and policy engine for modified files.
- Add integration tests for webhook event fixtures.
