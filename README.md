# DavosBot

Sanitized iMessage AI assistant runtime snapshot.

DavosBot runs on a Mac Mini, polls macOS Messages `chat.db`, routes messages through a local Ollama model with Gemini fallback/tool-use, and replies through AppleScript. It supports owner/admin/friend permissions, group-chat `@Davos` routing, personas, reminders, scheduled messages, recurring cron jobs, sports/social bets, weather/sports tools, live Mag 7/index tracking with extended-hours alerts, and maintenance logging.

This snapshot is detached from private runtime history. Review and configure it before deploying.

## Repo Map

| Path | Purpose |
|---|---|
| `main.py` | Thin compatibility entrypoint for PM2 and existing deploys |
| `davosbot/` | Runtime package |
| `davosbot/main.py` | Message polling, dispatch, background scheduler loops |
| `davosbot/brain.py` | LLM routing, Gemini tool loop, DB initialization, intent classifiers |
| `davosbot/commands.py` | Plain-text command dispatcher |
| `davosbot/tools.py` | LLM tool definitions and executors |
| `davosbot/permissions.py` | Owner/admin/friend permission tiers and secret redaction |
| `davosbot/memory.py` | Conversation history, reminders, user facts |
| `davosbot/personality.py` | System prompt builder, SOUL/MEMORY/persona loading |
| `davosbot/group_chat.py` | Group-chat state, approvals, personas, mention handling |
| `davosbot/imessage.py` | AppleScript sends and `chat.db` reads |
| `davosbot/config.py` | Environment loading, repo-root paths, and handle normalization |
| `tests/` | Regression tests for permissions, crons, personas, routing, safety |
| `scripts/` | Validation and sanitized public export helpers |

Fourth Down is maintained in the separate private `fourth-down` repository.
DavosBot keeps only the signed HTTP integration and configured production URL.
See `docs/REPOSITORIES.md` for the complete repository boundary.

## Runtime Files

These are intentionally ignored and should stay local to the machine running the bot:

- `.env`
- `MEMORY.md`
- `SOUL.md`
- `gc_state.json`
- `davosbot.db`
- `backups/`
- `generated/`
- local persona files under `personalities/*.md`

The checked-in `.example` files show the expected shape without real data.

## Working Workflow

1. Work from `C:\Users\<you>\davosbot` on Windows.
2. Keep changes small.
3. Run validation before pushing:

   ```powershell
   .\scripts\validate.ps1
   ```

4. Install local hooks once per clone:

   ```bash
   bash scripts/install_git_hooks.sh
   ```

5. Commit and push a `codex/*` task branch.
6. Let the GitHub fast integrator validate and fast-forward `master`.
7. If automatic deployment is not active, deploy by texting DavosBot:

   ```text
   pull
   ```

8. For runtime-sensitive changes, validate on the Mac Mini after the pull.

To refresh the shareable public snapshot after private validation:

```powershell
.\scripts\publish_public_snapshot.ps1 -DryRun
.\scripts\publish_public_snapshot.ps1 -SkipPush
.\scripts\publish_public_snapshot.ps1
```

## CI

GitHub Actions runs the same validation suite on every push to `master` and on pull requests:

```bash
bash scripts/validate.sh
```

The workflow uses read-only repository permissions and current GitHub-maintained Actions.

## Risk Levels

Green changes can usually be batched:

- docs
- tests
- README/help text
- validation scripts
- simple prompt wording

Yellow changes need a pull plus runtime smoke:

- persona UX
- cron list/edit UX
- model routing
- iMessage mention parsing
- image features

Red changes get isolated:

- permissions/admin gates
- `ADMIN_PASSWORD`
- private sends/outbound iMessage routing
- memory mutation
- reminders
- cron execution
- DB schema
- tool permission gates
- live self-edit/deploy automation

## Setup For A New Bot

This repo is not a polished public package, but the basic setup path is:

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp SOUL.example.md SOUL.md
cp MEMORY.example.md MEMORY.md
cp gc_state.example.json gc_state.json
cp personalities/example.md personalities/mypersona.md
pm2 start ecosystem.config.js
pm2 save
```

Fill in `.env`, `SOUL.md`, and `MEMORY.md` locally. Do not commit the filled-in files.
