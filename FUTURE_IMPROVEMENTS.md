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

## Prioritized Backlog (Highest to Lowest)

| Rank | Priority | Improvement | Impact | Complexity | Why this priority |
|---|---|---|---|---|---|
| 1 | P0 | Require webhook signature verification in production (fail closed if `GITHUB_WEBHOOK_SECRET` missing) | Very High | Low | Current code allows unsigned webhook acceptance when secret is empty; this is a direct trigger-spoofing risk. |
| 2 | P0 | Add webhook deduplication/idempotency with `X-GitHub-Delivery` and explicit supported `action` filters | Very High | Low | Prevents duplicate or irrelevant event execution and reduces queue noise/failure loops. |
| 3 | P0 | Add ingress hardening: request size limits + rate limiting on `/webhook/github` | Very High | Low-Medium | Reduces DoS risk from large payloads/event floods and protects worker capacity. |
| 4 | P0 | Make state machine explicit and documented; add `agent:needs-human` halt state for unsafe/ambiguous retries | Very High | Low-Medium | Improves reliability and prevents repeated autonomous failures without human intervention. |
| 5 | P0 | Enforce strict LLM output schema + action/path policy validation before any file edit | Very High | Medium | Core reliability control for malformed/unsafe LLM outputs in edit execution path. |
| 6 | P0 | Align egress controls (`proxy` allowlist + `NO_PROXY`) and add explicit `LLM_HOST_ALLOWLIST` config validation | High | Low | Prevents accidental over-permissive routing and broken LLM connectivity caused by config drift. |
| 7 | P0 | Add SSRF/internal network containment: block link-local, metadata, and RFC1918 destinations by default | High | Medium | Limits lateral movement/data access if prompts or dependencies attempt internal network access. |
| 8 | P0 | Run worker containers as non-root by default (keep existing read-only/caps-drop hardening) | High | Low | High-value isolation improvement with minimal operational overhead. |
| 9 | P1 | Use GitHub App installation tokens with minimal repo-scoped permissions; split read/write credentials | High | Medium | Reduces credential blast radius and aligns permissions with allowlisted repos. |
| 10 | P1 | Add clone transport policy: enforce HTTPS clone URL by default; reject unsupported SSH/private URL modes unless explicitly configured | High | Low-Medium | Prevents clone/push failures and hidden auth mismatches, especially for private repos. |
| 11 | P1 | Add LLM data-minimization controls: redact secrets/tokens/log fragments before prompt submission | High | Medium | Reduces exfiltration risk through the one allowed outbound channel (LLM endpoint). |
| 12 | P1 | Add structured logs, correlation IDs, and artifact hashing/evidence manifest | Medium-High | Medium | Improves incident response, trust, and run-level traceability. |
| 13 | P1 | Add integration tests for webhook fixtures plus security cases (unsigned webhook, oversize payload, replayed delivery ID) | Medium-High | Low-Medium | Ensures critical trigger/safety controls remain enforced over time. |
| 14 | P1 | Add semantic validation before commit (AST/static checks for changed languages) | README original | Medium-High | Medium | Catches structurally invalid edits that lint/tests may miss in partial repos. |
| 15 | P1 | Add context-evidence gate requiring cited files/paths (optional lines) before applying changes; route weak evidence to `agent:needs-human` | Medium-High | Medium | Reduces irrelevant edits and improves reviewability. |
| 16 | P2 | Add dependency-egress strategy: lockfiles, prebuilt worker image deps, optional internal package mirror | Medium-High | Medium-High | Prevents reopening broad internet egress just to satisfy install-time dependencies. |
| 17 | P2 | Add repository-specific policy bundles and persist quality-gate outputs as artifacts | README original + Table | Medium | Medium | Good for mature multi-repo use, but can overcomplicate early adoption if mandatory. |
| 18 | P2 | Split retry scheduler into a separate worker process/service (dead-letter API first, UI later) | Medium | Medium | Better fault isolation than in-process polling; not essential for low volume deployments. |
| 19 | P2 | Add distributed locking/queue-worker model for safe multi-instance scaling | Medium-High | High | Important once running multiple webhook instances; premature for single-instance setups. |
| 20 | P2 | Remove Docker socket from webhook service via dedicated runner service/job executor | High | High | Major security benefit but meaningful architecture and ops complexity increase. |
| 21 | P2 | Persist structured decision trace (plan, retrieval decisions, actions, checks, stop reasons) | Medium | Medium | Useful governance/review capability after core controls are in place. |
| 22 | P3 | Add durable SQL state for analytics/reporting alongside Redis operational state | README original | Medium | High | Helpful for reporting/compliance; not required for immediate reliability gains. |
| 23 | P3 | Add explicit agentic retrieval phase (repo/docs/history selection + retrieval logging) | Medium | Medium-High | Improves quality on complex tasks but increases orchestration complexity. |
| 24 | P3 | Add cross-run memory and post-merge feedback loop with memory governance controls | Low-Medium | High | Valuable long term; defer until baseline reliability/safety is consistently strong. |

## Scope Notes

- Complete all `P0` items before opening `P2/P3` workstreams.
- Favor simple OSS implementations first: FastAPI middleware/dependencies, Redis primitives, Squid/network policy, GitHub App APIs.
- Keep conservative secure defaults in `.env.example` and require explicit opt-out for risky modes.
