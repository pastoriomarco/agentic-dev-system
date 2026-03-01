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

## Practical deployment suggestions

- Run this service once (not once per target repo).
- Use one dedicated bot token or GitHub App installation per org/project.
- Keep this service on a stable host and expose `/webhook/github` using a reverse proxy.
- If testing locally, tunnel with `cloudflared` or `ngrok`.
- Restrict approval API access at network layer and with `ADMIN_API_TOKEN`.
- Set `ALLOWED_REPOS` explicitly in production.
: Example: `ALLOWED_REPOS=pastoriomarco/agentic-dev-system,pastoriomarco/private-repo`

## Current limitations

- No sandboxing per task container yet.
- Code generation is currently deterministic scaffolding in `agentic_changes/issue_<n>.md`.

## Next hardening steps

- Add durable SQL state for audit/reporting (Postgres) alongside Redis operational state.
- Add policy checks before PR creation.
- Add per-target-repo allowlist and path restrictions.
- Add integration tests for webhook event fixtures.
