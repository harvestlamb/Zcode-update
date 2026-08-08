---
name: code-review-checklist
description: Review code changes with a focused checklist for correctness, regressions, tests, and maintainability.
author: zcode-mirror
---

# Code Review Checklist

Use this skill when reviewing a pull request, merge request, or local diff.

## Focus areas

1. **Correctness** — Does the change do what it claims? Edge cases?
2. **Regressions** — Could existing callers break?
3. **Tests** — Are new paths covered? Missing fixtures?
4. **API / contracts** — Breaking changes, versioning, docs?
5. **Maintainability** — Naming, structure, unnecessary complexity?

## Output format

- Start with a one-line summary (approve / request changes / needs discussion).
- List findings as bullets, ordered by severity (blocker → nit).
- For each finding: file/area, why it matters, suggested fix when obvious.
- End with residual risks or follow-ups.
