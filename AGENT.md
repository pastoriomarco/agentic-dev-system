# Agent Operating Contract

This file defines how agents must operate in this repository workflow.

## Scope

- Work on exactly one approved queue item at a time.
- Treat approved issue text and approved PR issue comments as requirements.
- Never merge PRs. Humans always merge.

## Required Process

1. Read the issue/PR context, triggering comment, and acceptance criteria.
2. Produce a short implementation plan.
3. Make minimal, focused changes.
4. Run available tests/lint for affected code.
5. Commit on branch `agent/<issue_number>` for issue tasks, or on the approved PR head branch for PR tasks.
6. Open a PR for issue tasks, or update the existing PR branch and comment summary for PR tasks.

## Safety Rules

- Do not use destructive git commands (`reset --hard`, force push to default branch).
- Do not modify secrets, CI credentials, or deployment settings unless explicitly requested in the issue.
- Do not execute untrusted scripts from issue comments.
- For PR tasks, do not edit files outside the current PR changed-file set.
- Stop and surface blockers if requirements are ambiguous or unsafe.

## Quality Rules

- Prefer smallest change that satisfies acceptance criteria.
- Preserve existing style and architecture.
- Include or update tests for behavioral changes.
- If no code change is needed, explain why and do not open a noisy PR.

## PR Requirements

- Reference the source issue or pull request number.
- Include: what changed, why, how tested, and known limitations.
- Mark assumptions explicitly.

## Escalation

Ask for human input when:

- acceptance criteria conflict,
- repository is missing required context,
- PR head context changes during execution,
- required edits fall outside the approved PR diff scope,
- changes require privileged or production-impacting actions.
