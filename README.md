# Agentic Dev System (MVP)

Webhook-driven agent workflow for GitHub issues and a narrow, PR-aware task path with mandatory human approval before execution.

## Documentation

- Quickstart and overview: this `README.md`
- Full operating guide: [docs/USER_MANUAL.md](./docs/USER_MANUAL.md)
- Hardening roadmap: [FUTURE_IMPROVEMENTS.md](./FUTURE_IMPROVEMENTS.md)

## What it does

- Receives GitHub webhook events.
- Stores immutable task records keyed by webhook delivery ID.
- Waits for explicit approval.
- Runs one agent task at a time.
- Creates branch, commit, push, and PR for issue tasks.
- Updates the existing PR branch for supported pull request tasks.

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
- `WEBHOOK_MAX_BODY_BYTES`, `WEBHOOK_RATE_LIMIT_*` for webhook ingress limits
- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`
- `ADMIN_API_TOKEN` (required in production)
- `GITHUB_STATUS_*` and `GH_LABEL_*` for default GitHub-side ownership signaling
- `REDIS_URL` (defaults to local compose Redis)
- `ALLOWED_REPOS` (recommended for multi-repo safety)
- `ALLOWED_TRIGGER_USERS` and/or `ALLOWED_AUTHOR_ASSOCIATIONS` for sender-level webhook gating
- `MAX_RETRIES`, `RETRY_BASE_DELAY_SECONDS`, `RETRY_MAX_DELAY_SECONDS`, `RETRY_POLL_INTERVAL_SECONDS`
- `WORKER_*` limits and runtime controls (CPU/memory/pids/timeout/image/network, `WORKER_RUN_AS_UID`, `WORKER_RUN_AS_GID`, `WORKER_ENABLE_HOST_GATEWAY`)
- `LLM_API_URL`, `LLM_MODEL`, `LLM_HOST_ALLOWLIST`
- `AGENT_*` controls for changed-file policy, diff policy, quality gates, and `AGENT_MAX_EDIT_ACTIONS`
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
- Events: `issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`
- Supported actions: `issues.opened`, `issues.edited`, `issues.reopened`, `issue_comment.created`, `pull_request.synchronize`, `pull_request_review.submitted`, `pull_request_review_comment.created`
- Oversize webhook bodies are rejected with `413 payload_too_large`; fixed-window ingress throttling returns `429 rate_limited`.
- Public LLM hosts must be in `LLM_HOST_ALLOWLIST` and reachable through the Squid `allowed_domains` proxy path; private/local LLM hosts must also be present in `WORKER_NO_PROXY`.
- If `LLM_API_URL` uses `host.docker.internal`, set `WORKER_ENABLE_HOST_GATEWAY=true`; worker host-gateway exposure is otherwise disabled by default.
- PR issue comments create a task only when they contain an explicit `@agent` or `@ai` trigger.
- PR review comments and submitted review bodies also create tasks only with an explicit `@agent` or `@ai` trigger.
- Review-comment tasks are additionally scoped to the commented file and persisted with file/line context.
- Fork PR tasks are accepted too, but they publish follow-up helper PRs in the base repo instead of pushing to contributor branches.
- Task-creating events can also be restricted by GitHub login (`ALLOWED_TRIGGER_USERS`) and/or webhook `author_association` (`ALLOWED_AUTHOR_ASSOCIATIONS`, for example `OWNER,MEMBER,COLLABORATOR`).

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

List immutable tasks:

```bash
curl http://localhost:8000/api/tasks
```

Approve (starts agent):

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/issues/<queue_key>/approve
```

Approve a PR task by delivery/task id:

```bash
curl -X POST \
  -H "X-Admin-Token: <ADMIN_API_TOKEN>" \
  http://localhost:8000/api/tasks/<task_id>/approve
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
- The queue projection includes `subject_kind` to distinguish a plain issue task from a pull request task bound to the same GitHub number space.

## Operational model

- `AGENT.md` defines agent behavior and quality/safety rules.
- Only approved items are executed.
- Execution is serialized through an internal lock (one task at a time).
- Queue and session persistence are stored in Redis.
- Each webhook delivery is stored as its own task record keyed by `X-GitHub-Delivery`; the queue API exposes the current task projection per issue/PR number.
- Webhooks are accepted only from allowlisted repos.
: If `ALLOWED_REPOS` is empty, the service defaults to allowlisting `GITHUB_OWNER/GITHUB_REPO` when set.
- Task creation can be further restricted to specific GitHub logins and/or trusted GitHub author associations.
: If `ALLOWED_TRIGGER_USERS` is set, the webhook `sender.login` must match.
: If `ALLOWED_AUTHOR_ASSOCIATIONS` is set, the event author's GitHub `author_association` must match.
- In `production`, startup fails if `GITHUB_WEBHOOK_SECRET` is empty; unsigned webhooks are rejected.
- In `production`, startup also fails if `ADMIN_API_TOKEN` is empty.
- Webhook delivery IDs (`X-GitHub-Delivery`) are deduplicated in Redis to prevent replay/duplicate processing.
- `/webhook/github` enforces a request body cap and Redis-backed fixed-window rate limits before task creation.
- Startup validates `LLM_API_URL`, `LLM_HOST_ALLOWLIST`, proxy URLs, `WORKER_NO_PROXY`, and Squid `allowed_domains` so public and private LLM routes cannot drift silently.
- Startup also requires explicit `WORKER_ENABLE_HOST_GATEWAY=true` before a worker may route to `host.docker.internal`.
- Failed runs are retried with exponential backoff and moved to dead-letter after max retries.
- On startup, tasks left in `processing` are reconciled by re-ingesting finished worker artifacts when present; otherwise orphaned worker containers/volumes are cleaned up and the task is moved to `needs_human`.
- Supported PR tasks are bound to the PR head SHA; `pull_request.synchronize` or any head movement before publish moves the task to `needs_human`.
- Each approved issue runs inside a short-lived worker container with:
: read-only root filesystem, dropped Linux caps, no-new-privileges, CPU/memory/pids limits, dedicated per-task workspace volume, and non-root runtime UID/GID after a short root-owned mount-permission prep step.
- Worker containers are destroyed after execution; workspace volume is removed; artifacts/logs are retained.
- The orchestrator now performs repo-aware edits through LLM-generated file operations.
- LLM plan/edit responses are schema-validated; malformed or policy-violating edit payloads halt in `needs_human` before any file mutation.
- Commit is blocked unless quality gates pass and policy checks pass (changed file count, diff size, forbidden paths).
- For PR tasks, quality-gate subprocesses run without GitHub write credentials and edits are constrained to the PR's changed files.
- Link-local, metadata, and unintended private-network LLM destinations are blocked by network policy before any worker HTTP request is made.
- Agent permissions are explicitly provided from `AGENT_PERMISSIONS.md` and injected into LLM system prompts.
- GitHub issue status is signaled automatically via labels and comments (default labels):
: `agent:queued`, `agent:in-progress`, `agent:needs-human`, `agent:pr-opened`, `agent:failed`, `agent:dead-letter`, `agent:rejected`

## Practical deployment suggestions

- Run this service once (not once per target repo).
- Use one dedicated bot token or GitHub App installation per org/project.
- Keep this service on a stable host and expose `/webhook/github` using a reverse proxy.
- If testing locally, tunnel with `cloudflared` or `ngrok`.
- Restrict approval API access at network layer and set `ADMIN_API_TOKEN`.
- Set `ALLOWED_REPOS` explicitly in production.
: Example: `ALLOWED_REPOS=pastoriomarco/agentic-dev-system,pastoriomarco/private-repo`
- For public repos, set `ALLOWED_AUTHOR_ASSOCIATIONS=OWNER,MEMBER,COLLABORATOR` unless you intentionally want outside contributors to enqueue work.
- If you want only named maintainers or bots to trigger work, set `ALLOWED_TRIGGER_USERS=<login1>,<login2>`.
- Set `DEPLOYMENT_ENV=production` in deployed environments so signature checks fail closed on misconfiguration.
- This design requires Docker socket access by the webhook service.
: Treat that host as privileged infrastructure and isolate it from untrusted multi-tenant workloads.
- Worker internet egress is restricted through Squid proxy allowlist (`proxy/squid.conf`), defaulting to GitHub domains.
: To allow extra destinations (for example remote LLM APIs), update `proxy/squid.conf` explicitly.
- Keep `WORKER_ENABLE_HOST_GATEWAY=false` unless the worker must reach a host-local LLM through `host.docker.internal`.
- Use `/health/deep` for preflight checks (Redis, Docker daemon, proxy->GitHub, optional LLM endpoint).
- Optional auto-assignment is available (`GITHUB_ASSIGN_ON_PROCESSING=true` with `GITHUB_ASSIGNEE_LOGIN=<bot-user>`).

## Current limitations

- PR-aware execution is still intentionally narrow: PR issue comments, review comments, and submitted review bodies must contain explicit `@agent` or `@ai` triggers.
- Forked PR tasks do not update the contributor branch directly; they publish a helper PR in the base repo and comment back on the source PR.
- Review-comment tasks must stay within the commented file; broader multi-file refactors from a review comment should be routed through human review instead.
- LLM-generated edits can still fail on complex repos or ambiguous requirements; add repository-specific prompts/rules to improve reliability.
- Domain-level allowlisting cannot guarantee single-repo access by itself; enforce single-repo scope with GitHub App/fine-grained token permissions.

## License

Apache License 2.0. See [LICENSE](./LICENSE).

## Next hardening steps

See [FUTURE_IMPROVEMENTS.md](./FUTURE_IMPROVEMENTS.md) for the re-evaluated, prioritized roadmap (highest to lowest) with impact and complexity estimates.
