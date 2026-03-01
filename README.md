# AI Developer Agent System (MVP)

This project is a runnable MVP for an agentic developer workflow:

- Receives GitHub webhooks for issues/PRs/comments.
- Queues work items for human approval.
- Runs an agent in Docker after approval.
- Creates a branch, commit, and PR in GitHub.
- Keeps all merge decisions with humans.

## What This MVP Includes

- `webhook_handler.py`: FastAPI webhook + queue + approval API.
- `agent_orchestrator.py`: Clone repo, create deterministic change artifact, commit, push, open PR.
- `Dockerfile` + `docker-compose.yml`: containerized runtime.
- In-memory queue/session storage (no Redis persistence yet).

## Quick Start

1. Configure environment:

```bash
cd agentic-dev-system
cp .env.example .env
```

2. Edit `.env`:

- Set `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO`.
- Set `GITHUB_WEBHOOK_SECRET` to your webhook secret.
- Optionally set `GITHUB_BASE_BRANCH` (default `main`).

3. Start:

```bash
docker-compose up -d --build
```

4. Configure GitHub webhook:

- URL: `https://your-domain/webhook/github`
- Content type: `application/json`
- Secret: same as `.env`
- Events: `issues`, `pull_request`, `issue_comment`, `pull_request_review`, `pull_request_review_comment`

## API

- Health:

```bash
curl http://localhost:8000/health
```

- List queue:

```bash
curl http://localhost:8000/api/issues
```

- Approve and start agent:

```bash
curl -X POST http://localhost:8000/api/issues/{queue_key}/approve
```

- Reject:

```bash
curl -X POST http://localhost:8000/api/issues/{queue_key}/reject
```

- Session results:

```bash
curl http://localhost:8000/api/sessions
```

`queue_key` format is `owner:repo:issue_number` (example: `octocat:hello-world:42`).

## Current Trigger Rules

Work is marked `auto` when title/body/comment contains one of:

- `@agent`
- `@ai`
- `fix this`
- `implement`
- `create`

All items still require explicit approval through the API before execution.

## Current Agent Behavior

This MVP does not use autonomous code generation yet. It creates a deterministic artifact in the target repo:

- `agentic_changes/issue_<number>.md`

That guarantees a reproducible commit/PR flow while you integrate your LLM editing strategy.

## Notes

- Queue and sessions are in memory; restart clears state.
- If `GITHUB_WEBHOOK_SECRET` is empty, signature validation is disabled.
- For production, add persistent queue/state, retries, auth on approval endpoints, and least-privilege GitHub App auth.
