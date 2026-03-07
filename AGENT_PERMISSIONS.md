# Agent Runtime Permissions

This document is provided to the coding agent at runtime.

## What the agent can do

- Read and modify files inside the task-specific workspace.
- Use git in the cloned target repository.
- Push branch changes to the configured GitHub repository.
- Open pull requests for issue tasks on the configured repository.
- Update the existing branch and comment on the existing PR for supported same-repo PR tasks.

## What the agent cannot do

- Merge PRs.
- Access repositories outside token permissions.
- Access arbitrary internet destinations directly.

## Network policy

- Worker containers are attached to an isolated internal network.
- Outbound web access is forced through an egress proxy.
- Proxy allowlist is restricted to GitHub domains by default.
- Additional destinations require explicit operator configuration.

## File policy

- Forbidden paths and allowlisted prefixes are enforced by orchestrator policy checks.
- Diff size and changed-file-count limits are enforced before commit.
- For PR tasks, edits outside the PR's current changed-file set are blocked and escalated to `needs_human`.
- Quality gates must pass before commit unless explicitly overridden.
