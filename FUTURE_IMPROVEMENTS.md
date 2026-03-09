# Future / Planned Improvements

This backlog is re-evaluated for:

- simplicity of operation/configuration,
- free/open-source-first implementation choices,
- reliability and security for both private and public repos.

## Priority Legend

- `P0`: Must-do now (high risk reduction, low/medium complexity)
- `P1`: Next important (high value, moderate effort)
- `P2`: Later/scale-up (useful but heavier or context-dependent)
- `P3`: Future/optional (defer until core is stable)

## Implementation Status (Updated: 2026-03-07)

- Completed:
  - `Rank 1 / P0` Require webhook signature verification in production (fail closed if `GITHUB_WEBHOOK_SECRET` missing).
  - `Rank 2 / P0` Add webhook deduplication/idempotency with `X-GitHub-Delivery` and explicit supported `action` filters.
  - `Rank 3 / P0` Make webhook acceptance durable before returning `200` (persist immutable task first, then acknowledge delivery).
  - `Rank 4 / P0` Require approval API authentication in production (fail closed if `ADMIN_API_TOKEN` is missing).
  - `Rank 5 / P0` Add ingress hardening: request size limits + rate limiting on `/webhook/github`.
  - `Rank 6 / P0` Split immutable task records from mutable issue state so approvals and retries operate on a specific task ID.
  - `Rank 7 / P0` Make the state machine explicit, validate legal transitions, and use `needs_human` as a safe halt state.
  - `Rank 8 / P0` Deliver the minimum valid PR-aware path: same-repo PR issue comments with explicit `@agent`/`@ai` trigger, PR head/base binding, task-level approvals, update-in-place on the PR branch, and stale-task handling on `pull_request.synchronize`.
  - `Rank 31 / P0` Support same-repo review-driven PR tasks: accept `pull_request_review_comment.created` and `pull_request_review.submitted` with explicit `@agent`/`@ai`, persist review file-line/body context, and restrict review-comment edits to the commented file.
  - `Rank 32 / P0` Add fork-safe PR handling for PR-aware tasks with helper-branch/helper-PR publish mode.
  - `Rank 10 / P0` Enforce strict LLM output schema + action/path policy validation before any file edit.
  - `Rank 11 / P0` Align egress controls (`proxy` allowlist + `NO_PROXY`) and add explicit `LLM_HOST_ALLOWLIST` config validation.
  - `Rank 12 / P0` Add scoped outbound endpoint validation for LLM/deep-health URLs: block metadata/link-local targets and reject unintended private-network routing for configured service endpoints.
  - `Rank 30 / P0` Make worker host-gateway exposure opt-in; inject `host.docker.internal` and direct-route bypass only for explicit local-host LLM mode.
  - `Rank 13 / P0` Run worker containers as non-root by default after a short-lived root-owned mount-permission prep step.
  - `Rank 9 / P0` Extend startup reconciliation from task-state recovery to detached worker containers/artifacts and session re-ingestion.
- Next up:
  - `Recommended next session / P1` Use GitHub App installation tokens with minimal repo-scoped permissions and split read/write credentials.
  - `Rank 14 / P1` Use GitHub App installation tokens with minimal repo-scoped permissions; split read/write credentials.

## Backlog By Priority Level

The table below is ordered by `Priority` first and only includes open backlog items. Original rank references are preserved for traceability; completed work is tracked only in the implementation status section above.

| Rank | Priority | Improvement | Impact | Complexity | Why this priority |
|---|---|---|---|---|---|
| 14 | P1 | Use GitHub App installation tokens with minimal repo-scoped permissions; split read/write credentials | High | Medium | Reduces credential blast radius and aligns permissions with allowlisted repos. |
| 15 | P1 | Add clone transport policy: enforce HTTPS clone URL by default; reject unsupported SSH/private URL modes unless explicitly configured | High | Low-Medium | Prevents clone/push failures and hidden auth mismatches, especially for private repos. |
| 16 | P1 | Add LLM data-minimization controls: redact secrets/tokens/log fragments before prompt submission | High | Medium | Reduces exfiltration risk through the one allowed outbound channel (LLM endpoint). |
| 17 | P1 | Add structured logs, correlation IDs, and artifact hashing/evidence manifest | Medium-High | Medium | Improves incident response, trust, and run-level traceability. |
| 18 | P1 | Add integration tests for webhook fixtures plus security cases (unsigned webhook, oversize payload, replayed delivery ID) | Medium-High | Low-Medium | Ensures critical trigger/safety controls remain enforced over time. |
| 19 | P1 | Add semantic validation before commit (AST/static checks for changed languages) | README original | Medium-High | Medium | Catches structurally invalid edits that lint/tests may miss in partial repos. |
| 20 | P1 | Add context-evidence gate requiring cited files/paths (optional lines) before applying changes; route weak evidence to `agent:needs-human` | Medium-High | Medium | Reduces irrelevant edits and improves reviewability. |
| 21 | P2 | Add dependency-egress strategy: lockfiles, prebuilt worker image deps, optional internal package mirror | Medium-High | Medium-High | Prevents reopening broad internet egress just to satisfy install-time dependencies. |
| 22 | P2 | Add repository-specific policy bundles and persist quality-gate outputs as artifacts | README original + Table | Medium | Medium | Good for mature multi-repo use, but can overcomplicate early adoption if mandatory. |
| 23 | P2 | Split retry scheduler into a separate worker process/service (dead-letter API first, UI later) | Medium | Medium | Better fault isolation than in-process polling; not essential for low volume deployments. |
| 24 | P2 | Add distributed locking/queue-worker model for safe multi-instance scaling | Medium-High | High | Important once running multiple webhook instances; premature for single-instance setups. |
| 25 | P2 | Remove Docker socket from webhook service via dedicated runner service/job executor | High | High | Major security benefit but meaningful architecture and ops complexity increase. |
| 26 | P2 | Persist structured decision trace (plan, retrieval decisions, actions, checks, stop reasons) | Medium | Medium | Useful governance/review capability after core controls are in place. |
| 27 | P3 | Add durable SQL state for analytics/reporting alongside Redis operational state | README original | Medium | High | Helpful for reporting/compliance; not required for immediate reliability gains. |
| 28 | P3 | Add explicit agentic retrieval phase (repo/docs/history selection + retrieval logging) | Medium | Medium-High | Improves quality on complex tasks but increases orchestration complexity. |
| 29 | P3 | Add cross-run memory and post-merge feedback loop with memory governance controls | Low-Medium | High | Valuable long term; defer until baseline reliability/safety is consistently strong. |

## Scope Notes

- Complete all `P0` items before opening `P2/P3` workstreams.
- Favor simple OSS implementations first: FastAPI middleware/dependencies, Redis primitives, Squid/network policy, GitHub App APIs.
- Keep conservative secure defaults in `.env.example` and require explicit opt-out for risky modes.
