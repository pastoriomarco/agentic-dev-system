# Agent Runtime Permissions

This document is provided to the coding agent at runtime.

## What the agent can do

- Read and modify files inside the task-specific workspace.
- Use git in the cloned target repository.
- Push branch changes to the configured GitHub repository.
- Open pull requests on the configured repository.

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
- Quality gates must pass before commit unless explicitly overridden.
