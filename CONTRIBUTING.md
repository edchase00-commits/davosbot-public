# Contributing

This repo is the owner's live iMessage bot. Treat changes as production-adjacent even when editing from the Windows mirror.

## Workflow

1. Work from `C:\Users\<you>\davosbot` on Windows or `/Users/<you>/projects/davosbot` on the Mac Mini.
2. Do not use the stale mirror at `C:\Users\<you>\projects\davosbot`.
3. Inspect `AGENTS.md`, `docs/README.md`, `docs/TASKS.md`, and the relevant source files before changing behavior.
4. Keep changes small and reversible.
5. Run validation before pushing:

   ```bash
   bash scripts/validate.sh
   ```

   On Windows:

   ```powershell
   .\scripts\validate.ps1
   ```

6. Push with `deploy-davos "message"` from Windows when available.
7. Deploy runtime changes by texting DavosBot `pull`.

## Commit Messages

Use Conventional Commits for new commits:

```text
type(scope): summary
```

Allowed types are `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
`test`, `build`, `ci`, `chore`, and `ops`. Scope is optional.

Examples:

```text
fix: harden reminder save confirmation
feat(images): add provider router
chore: refresh future queue
```

Install repo-managed hooks with:

```bash
scripts/install_git_hooks.sh
```

## Risk Colors

- Green: docs, tests, help text, CI, validation scripts, and simple prompt wording.
- Yellow: features that need live chat/runtime smoke after pull, such as weather, betting UX, persona UX, morning quote behavior, and image generation.
- Red: permissions, memory mutation, reminder/cron routing, iMessage outbound sends, DB schema changes, tool gates, `ADMIN_PASSWORD`, `MEMORY.md`, `SOUL.md`, and `gc_state.json`.

Red changes get one isolated commit, one pull, and Mini validation.

## Secrets

Never print or commit tokens, API keys, passwords, phone-number dumps, `MEMORY.md`, `SOUL.md`, `.env`, local agent config directories, `gc_state.json`, database files, or generated personal files.
