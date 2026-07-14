## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues using the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the default five-role triage label vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

This repository uses the single-context domain documentation layout. See `docs/agents/domain.md`.

### Architecture and code flow

Before changing application workflows, background-task scheduling, data ownership,
database schemas, provider interfaces, or CLI entry points, read
`docs/architecture/code-flow.md`.

Any implementation change that affects those areas must update
`docs/architecture/code-flow.md` in the same change. Work is incomplete when the
documented flow and the code disagree.
