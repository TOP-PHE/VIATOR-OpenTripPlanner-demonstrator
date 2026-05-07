---
name: Operations incident
about: Production / deploy issue (containers crashed, journey search not working, deploy broke X)
title: "ops: "
labels: ["ops", "incident"]
---

<!-- Audit-2026-05 #11. Designed to capture incident triage in one place
     so post-mortems are quick to write and patterns are easy to spot.
     Mirror the structure even when filling it in mid-incident — partial
     fields are better than no fields. -->

## Summary

<!-- One sentence: what stopped working, when, blast radius. -->

## Severity

- [ ] **SEV-1** — production unavailable, data at risk
- [ ] **SEV-2** — feature degraded, workaround exists
- [ ] **SEV-3** — operator-noticeable but no user impact

## Timeline

<!-- ISO timestamps + one line per event. Append as you investigate. -->

- `YYYY-MM-DD HH:MM Z` — symptom first observed
- `YYYY-MM-DD HH:MM Z` — root cause identified
- `YYYY-MM-DD HH:MM Z` — recovery action started
- `YYYY-MM-DD HH:MM Z` — service restored

## Symptoms

<!-- What was/is operator- or user-visible. Concrete commands & output, not paraphrase. -->

```
```

## Affected components

- [ ] web (`viator-web-1`)
- [ ] worker (`viator-worker-1`)
- [ ] nginx (`viator-nginx-1`)
- [ ] postgres (`viator-postgres-1`)
- [ ] per-session OTP container(s): <!-- list session ids -->
- [ ] CI / GitHub Actions
- [ ] SonarCloud / external integration: <!-- which -->

## Diagnostic steps run

<!-- The commands you ran, in order. Paste outputs that mattered.
     Reference admin-guide.md sections used (e.g. "ran §6.7 / §6.11 step 7"). -->

```
```

## Root cause

<!-- One paragraph. Don't write "the system failed"; write what specifically failed and why.
     If unknown when filing, write "TBD" and update post-recovery. -->

## Recovery actions

<!-- Concrete steps that restored service. Useful to copy-paste in a
     future incident's diagnostic-steps section if symptoms repeat. -->

```
```

## Followups

<!-- One or more of:
     - audit-2026-05 item to file (or reference existing)
     - admin-guide.md runbook addition
     - code change to prevent recurrence
     Convert each to a separate Issue or PR if non-trivial. -->

- [ ]
- [ ]

## Communication

<!-- For SEV-1/SEV-2: who was notified, when, and what they were told.
     n/a for SEV-3. -->

n/a
