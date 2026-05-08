# Architecture Decision Records (ADRs)

Each file in this directory captures a single architectural decision: why
we made it, what we considered, and what trips it should be revisited.

ADRs are append-only — when a decision is later reversed, write a new ADR
that links back and explains the change. Don't edit the original.

## Naming

`NNNN-kebab-case-title.md` — zero-padded, sequential. Bump the number for
each new ADR. Don't reuse numbers, even if an ADR is superseded.

## Format

The standard Michael-Nygard skeleton:

- **Status** — Proposed / Accepted / Deprecated / Superseded by NNNN
- **Context** — what's the situation, what forces are at play
- **Decision** — what we're actually doing
- **Consequences** — what gets easier / harder as a result
- **Trigger to revisit** *(VIATOR-specific addition)* — explicit signals
  that should prompt re-opening the decision. This is the bit that prevents
  ADRs from becoming write-only artifacts: if the trigger fires, an
  operator/maintainer reopens the file, considers the new context, and
  either re-affirms the decision (note added) or files a superseding ADR.

## Index

| Number | Title | Status |
|--------|-------|--------|
| 0001 | [Sync SQLAlchemy under async FastAPI](./0001-sync-sqlalchemy-under-async-fastapi.md) | Accepted |
