---
name: release-notes
description: Draft clear release notes from commits, PRs, or a changelog diff for engineers and users.
author: zcode-mirror
---

# Release Notes

Use this skill when writing release notes, changelog entries, or update announcements from recent commits or PRs.

## Process

1. Gather the change set (commits, PR titles, or `git log` range).
2. Group into: **Features**, **Fixes**, **Improvements**, **Breaking**.
3. Rewrite each item in plain language — what users gain, not only what files changed.
4. Call out migrations, config changes, or required actions.

## Output template

```markdown
## What's new

- …

## Fixes

- …

## Notes / migrations

- …
```

Keep tone concise. Prefer user-visible outcomes over internal refactors unless they matter operationally.
