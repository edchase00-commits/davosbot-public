# DavosBot Public Snapshot

This directory is safe for the sanitized public snapshot. It is intentionally
separate from the private operator docs.

## What Is Included

- Runtime package code needed to understand the bot architecture.
- Example config files with empty placeholders.
- Tests and validation helpers.
- Public-safe setup and architecture notes.

## What Is Excluded

- API keys, tokens, webhook URLs, and filled `.env` files.
- Phone numbers, Apple IDs, private handles, and private chat logs.
- Live `MEMORY.md`, `SOUL.md`, private persona files, and local bot state.
- Databases, generated images/files, backups, PM2 logs, and private exports.
- Private operator handoffs, incident notes, and machine-specific paths.

## Release Checklist

Before publishing a public snapshot:

1. Run `.\scripts\publish_public_snapshot.ps1 -DryRun`.
2. Review the allowlisted files shown in the dry run.
3. Run `.\scripts\publish_public_snapshot.ps1 -SkipPush` for local validation.
4. Confirm compile, tests, cleanup, and private-marker scan pass.
5. Review the generated snapshot diff before any public push.

Publishing must stay explicit. Do not wire public export to an automatic push
hook from the private runtime repository.
