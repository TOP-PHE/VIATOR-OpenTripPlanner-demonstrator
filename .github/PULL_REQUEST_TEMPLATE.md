<!--
PR template — VIATOR / OpenJourneyPlanner. Audit-2026-05 #11.

Keep the structure even when the change is tiny. The "Test plan" section
in particular is what made the audit-2026-05 PRs reviewable — every PR
since v0.1.32.3 has had it, and post-merge incident triage has been
materially faster as a result.

Delete sections that genuinely don't apply (e.g. "Database migration"
on a docs-only change). Don't delete the heading just because it's
empty — write "n/a" so reviewers know you considered it.
-->

## Summary

<!-- 1-3 bullet points. What and why, not how. -->

-
-

## Audit / issue reference

<!-- If this PR closes an audit-2026-05 item, link it:
       Closes audit-2026-05.md item #N
     If it closes a GitHub issue:
       Closes #NNN
     If neither, write "Standalone change" and explain. -->

## What changed

<!-- File-by-file or component-by-component summary for reviewers.
     For bigger PRs, mention the order they should be read in. -->

## Test plan

<!-- Checkboxes the reviewer (and CI) tick off before merge.
     Required CI gates are listed here so the merger doesn't
     forget what's actually being asserted. -->

- [ ] `pre-commit` is green (ruff + format + hadolint + EOF + YAML)
- [ ] `Python (lint + type + test)` is green (mypy strict + ruff + bandit + pip-audit + pytest with coverage floor)
- [ ] `Docker / Build, scan, push (web)` is green (Trivy gate)
- [ ] `Docker / Build, scan, push (otp)` is green (Trivy gate)
- [ ] `SonarCloud Code Analysis` is green (Quality Gate, no new issues, no new unreviewed Security Hotspots)

<!-- For changes that affect runtime (deploy or per-session container behaviour), also: -->
- [ ] **n/a OR** smoke-tested locally with `docker compose up -d` + a Paris-Lyon journey search
- [ ] **n/a OR** tested non-root container UID is preserved (`docker exec viator-web-1 id` → uid=1000)

## Behaviour-change risk

<!-- One sentence per. -->

- **Runtime behaviour**: <!-- e.g. "no change" / "adds new env var X with default Y" -->
- **Database schema**: <!-- e.g. "no migration" / "adds nullable column foo to bar" -->
- **Operator-visible**: <!-- e.g. "no UI change" / "new admin page at /admin/X" -->
- **Deploy-time**: <!-- e.g. "drop-in replacement" / "operator must run admin-guide §6.X step before pull" -->

## Migration notes

<!-- Anything an operator needs to know BEFORE merging or BEFORE the
     next deploy. Write `n/a` if there's nothing.
     If the change requires a coordinated host-side step (chown'ing
     a bind mount, setting a new env var in `.env`, etc.), document it
     here and mirror the doc in admin-guide.md. -->

n/a

## Post-merge

<!-- Anything that needs to happen after merge but before/at the
     next release. Tag bump, runbook update, etc. -->

- [ ] Cut release tag if user-visible: `git tag -a v0.1.X.Y -m "..." && git push origin v0.1.X.Y`
- [ ] Update `docs/audit-2026-05.md` tracker if this closed an item

🤖 Audit-2026-05 PR template
