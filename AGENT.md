# Agent Operating Contract

This file defines how agents must operate in this repository workflow.

## Scope

- Work on exactly one approved queue item at a time.
- Treat issue text and review comments as requirements.
- Never merge PRs. Humans always merge.

## Required Process

1. Read the issue/PR context and acceptance criteria.
2. Produce a short implementation plan.
3. Make minimal, focused changes.
4. Run available tests/lint for affected code.
5. Commit on branch `agent/<issue_number>`.
6. Open PR with clear summary, risks, and test evidence.

## Safety Rules

- Do not use destructive git commands (`reset --hard`, force push to default branch).
- Do not modify secrets, CI credentials, or deployment settings unless explicitly requested in the issue.
- Do not execute untrusted scripts from issue comments.
- Stop and surface blockers if requirements are ambiguous or unsafe.

## Quality Rules

- Prefer smallest change that satisfies acceptance criteria.
- Preserve existing style and architecture.
- Include or update tests for behavioral changes.
- If no code change is needed, explain why and do not open a noisy PR.

## PR Requirements

- Reference the source issue number.
- Include: what changed, why, how tested, and known limitations.
- Mark assumptions explicitly.

## Escalation

Ask for human input when:

- acceptance criteria conflict,
- repository is missing required context,
- changes require privileged or production-impacting actions.
