# DavosBot — Personal AI Assistant

Always-on iMessage bot running on a Mac Mini M4. the owner's personal assistant and everyone's friend in the group chat.

**No `!` prefix anywhere.** All commands are plain English words.

---

## Architecture

| File | Role |
|---|---|
| `main.py` | Entry point (PM2). Polls chat.db, dispatches to `handle_dm` / `handle_group_message`. Runs `_check_scheduled_tasks`, `_check_reminders`, `_check_cron_jobs` every loop tick. |
| `brain.py` | LLM routing (Ollama → Gemini fallback). `_init_db_tables`, agentic tool loop, rate limiting, session tracking, intent classifiers. |
| `commands.py` | Plaintext command dispatcher for DM and GC. Every command wired without `!` prefix. |
| `permissions.py` | Three-tier auth: `is_owner`, `is_admin`, `can_user_do`. |
| `tools.py` | LLM tool definitions + executors (`send_imessage`, `set_reminder`, etc). |
| `memory.py` | Conversation history, reminders, `get_due_reminders`. |
| `personality.py` | Loads SOUL.md + persona files; builds system prompts. |
| `group_chat.py` | `@Davos` command handler, GC state via `gc_state.json`. |
| `soul.py` | Versioned SOUL.md writes with backup. |
| `db.py` | `run_migration` and DB backup utilities. |
| `imessage.py` | iMessage send/receive via AppleScript + chat.db polling. |
| `config.py` | Env var loading (`OWNER_ID`, `GEMINI_API_KEY`, SMTP, etc). |
| `market.py` | Current/extended-hours stock quotes, deterministic market queries, and deduped owner price alerts. |

---

## Permission Tiers

| Tier | How granted | Capabilities |
|---|---|---|
| **Owner** | `OWNER_ID` in `.env` | Everything, unrestricted. |
| **Admin** | `grant [handle]` (owner only) | All commands except `OWNER_ONLY_ACTIONS` (deploy, view_logs, view_billing, manage_memory, view_chats, view_changelog, view_session, view_personalities, view_backups, manage_ratelimit, modify_soul, change_personality, schedule_cron, grant_admin, revoke_admin, view_audit_log). |
| **Friend** | `@Davos allow [handle]` (owner only) | Conversational LLM responses; bets listing; help and mypermissions. No admin commands. |
| **Unknown** | — | "You're not on the list. Talk to the owner." |

---

## Command Reference (DM, plaintext — no `!`)

### Natural-language features (no slash, no syntax)

These are NOT plaintext commands — the LLM detects intent and calls a tool. Phrasing is fluid.

| Capability | Example phrasings | Tool |
|---|---|---|
| Set reminder | "remind me to X in Y", "remind me tomorrow at 3pm to Z" | `set_reminder` |
| List reminders | "what reminders do i have?", "list my reminders" | `list_reminders` |
| Cancel reminder | "cancel reminder 2", "cancel my 3pm", "drop the gym one" | `cancel_reminder` (1-based position; internal id hidden) |
| Edit reminder | "move my 3pm to 4pm", "change tomorrow's gym to 7am" | `handle_reminder_edit` (cancel + recreate flow) |
| Schedule daily job | "every morning at 6:30 PT send 'good morning boys' + a quote" | `schedule_cron(time_pt, intro)` |
| List daily jobs | "what daily jobs do we have?", "list crons" | `list_crons` |
| Cancel daily job | "cancel the 6:30 daily", "kill the morning job" | `cancel_cron` (1-based position) |
| Send / schedule private iMessage | "text Cole 'happy birthday' tomorrow at 9am" | `send_imessage` creates a pending confirmation; password reply required before send/schedule |
| Persona switch | "switch to gruden", "be jarjar", "activate atl" | NL intent classifier → `_cmd_persona` |
| Market data | "how's NVDA?", "how's Mag 7?", "what moved after hours?" | Deterministic market query; explanatory/news follow-ups use existing web search |

All NL features routed via the originating chat (`originating_chat_id`) — DMs stay in DM, GCs stay in GC, nothing leaks across.

### System (Owner)

| Command | Access | What it does | Example |
|---|---|---|---|
| `status` | Owner | PM2 process info + DB session data in one combined output | `status` |
| `uptime` | Owner | How long the current session has been running | `uptime` |
| `logs` | Owner | Last 20 PM2 log lines | `logs` |
| `pull` | Owner | git pull + pm2 restart (self-deploys) | `pull` |
| `billing` | Owner | Gemini token usage + estimated paid-tier cost this month | `billing` |
| `backups` | Owner | Last 5 DB backups with size and timestamp | `backups` |

### Memory (Owner)

| Command | Access | What it does | Example |
|---|---|---|---|
| `memory` | Owner | Show current MEMORY.md | `memory` |
| `memory wipe` | Owner | Reset MEMORY.md to baseline facts | `memory wipe` |
| `memory add [fact]` | Owner | Append a single fact | `memory add the owner started lifting again` |
| `memory clear` | Owner | Wipe full conversation history | `memory clear` |
| `memory clear 30m` | Owner | Clear last 30 minutes of chat | `memory clear 30m` |
| `memory clear 10` | Owner | Clear last 10 messages | `memory clear 10` |
| `myfacts` | Owner | List all extracted user_facts from self-descriptions | `myfacts` |
| `enrichsoul` | Owner | Append user_facts as "Known about the owner" section to SOUL.md | `enrichsoul` |

### Persona (Owner)

| Command | Access | What it does | Example |
|---|---|---|---|
| `persona` | Owner | Show current active persona + all available | `persona` |
| `persona [name]` | Owner | Switch active DM persona | `persona gruden` |
| `persona reset` | Owner | Return to SOUL.md (default) | `persona reset` |
| `switch to [name]` | Owner | Natural-language persona switch (intent classifier) | `switch to gruden` |
| `be [name]` | Owner | Natural-language persona switch | `be jarjar` |
| `activate [name]` | Owner | Natural-language persona switch | `activate atl` |
| `soulversion` | Owner | Last 5 SOUL.md write history entries | `soulversion` |
| `restoresoul [file]` | Owner | Restore SOUL.md from a named backup | `restoresoul SOUL_20260428_183000.md` |
| `personalities` | Owner | List persona files with size and validation status | `personalities` |

### Admin (Owner)

| Command | Access | What it does | Example |
|---|---|---|---|
| `grant [handle]` | Owner | Elevate to admin tier; also adds to `approved_users` so they're visible in GCs | `grant <phone>` |
| `revoke [handle]` | Owner | Set `revoked_at` on admin record. Does NOT remove from `approved_users` — admin demotes to friend, not banished. | `revoke <phone>` |
| `admins` | Owner | List all active admins | `admins` |
| `ratelimit [handle]` | Owner | Show how many messages that handle sent in the last hour | `ratelimit <phone>` |
| `mypermissions` | **Open** | Show your access tier and what you can do | `mypermissions` |
| `capabilities` | **Open** | Show the major feature map, active image providers, and exact commands/conditions | `capabilities` |
| `changelog` | Owner | Alias for `log` | `changelog` |
| `log` | Owner | Show the change log | `log` |
| `log [text]` | Owner | Write a new entry to the change log | `log fixed the reminder routing bug` |
| `log board` | Owner | Show the compact GREEN/YELLOW/RED board | `log board` |
| `log plan` / `log triage` / `log safe cleanup` | Owner | Alias to the full safe-cleanup Codex handoff | `log safe cleanup` |
| `log remove [id]` / `log done [ids]` | Owner | Delete one or more completed change log entries by ID | `log done #7 #8 #9` |
| `log export` | Owner | Write a full private Markdown board to `exports/private/change_log_board.md` for SSH/Codex handoff | `log export` |
| `log clear` | Owner | Wipe all entries (requires confirm step within 60s) | `log clear` then `log clear confirm` |
| `ship safe cleanup` | Owner | Build a copy-paste Codex prompt from `change_log`; never edits, commits, pushes, deploys, changes runtime config, or clears rows by itself | `ship safe cleanup` |
| `big change [idea]` | Owner | Log a structured review-only Codex intake for a large idea | `big change add image generation but do not ship it yet` |
| `codex plan [idea]` | Owner | Alias for review-only big-change intake | `codex plan fix reminders safely` |
| `intake [idea]` | Owner | Alias for review-only big-change intake | `intake investigate the g error` |
| `analyze/log/fix/ship repair phrases` | Owner | Create one guarded self-repair row with message text, optional image scan result, recent log metadata, DB row snapshots, likely code area, and expected behavior | `log this and fix it` / `ship this cron fix` |
| `fix yourself: [what went wrong]` | Owner | Self-diagnose bot feedback and log the same review-only Codex repair handoff | `fix yourself: you guessed instead of asking for the sheet` |
| `self review [issue]` | Owner | Alias for fix-yourself repair intake | `self review why the reminder answer was wrong` |
| oversized owner setup/repair asks | Owner | If a message is too large for one model pass, log a review-only Codex intake with a redacted preview, length, risk, and safe pipeline | paste large ask |
| `gpt scan image [ask]` / `what's in this screenshot?` | Approved users | Scan an attached/buffered image through the configured image provider; daily limited and never prints raw media. `gpt scan` is legacy wording: the active provider may be Gemini if OpenAI is not configured. | `gpt scan image read the screenshot` |
| `image gen [prompt]` | Approved users | Generate an image through `IMAGE_PROVIDER`, save it under ignored `generated/images/`, and queue it as an attachment | `image gen a clean DavosBot logo concept` |

**Image capability truth:**
- DavosBot can analyze images when the image is attached or buffered in the same chat.
- DavosBot can generate images when `IMAGE_PROVIDER` has an available provider (`local`, `gemini`, or `openai`).
- The command name `gpt scan image` does not mean the OpenAI API key is configured; provider routing can use Gemini or another configured provider.
- If there is no attached/buffered image, say that an image is needed. Do not claim image analysis is impossible.

**Model routing truth:**
- Exact commands `model status`, `model options`, and `model intensity` are deterministic owner/admin status commands. Natural model questions like "which model do you use?", "model options", "routing status", and "model power rankings" should route to the same command path, not normal chat.
- Plain/simple chat uses local Ollama Gemma3 via `OLLAMA_SIMPLE_CHAT_MODEL` / `MODEL_ROUTE_SIMPLE_CHAT` because Gemma4 is not reliable for normal chat on the Mini yet. `OLLAMA_MODEL=gemma4` can remain installed/configured as the keep-warm/default local model target, but Gemma4 is parked until it stops returning empty or timing out on ordinary prompts. If local chat hard-fails, Davos silently falls back to Gemini via `GEMINI_MODEL`, currently `gemini-3.1-flash-lite`; canned Ollama refusals/provider-status narration also retry Gemini as soft misses. the owner is only alerted if hard local failure and Gemini fallback both fail.
- Tool-use/function-calling uses Gemini via `GEMINI_MODEL`, currently `gemini-3.1-flash-lite`. Model choice does not bypass owner/admin/friend gates or exposed-tool restrictions.
- Helper rewrite calls use `GEMINI_REWRITE_MODEL`, defaulting to `GEMINI_MODEL` when unset.
- Complex owner-only planning and code-review routes can use `ADVANCED_TEXT_MODEL` / `ADVANCED_CODE_MODEL`, currently `gemini-3.5-flash`, when the route label resolves to Gemini. These routes do not authorize live self-edit, deploy, private sends, file writes, DB writes, cron/reminder execution, or permission changes.
- Image generation uses `IMAGE_PROVIDER`. With `IMAGE_PROVIDER=auto`, Davos chooses local Flux through `LOCAL_IMAGE_ENDPOINT` first, then Gemini image fallback. OpenAI is not used by auto routes; it is legacy explicit only if configured.
- Image scan uses `IMAGE_SCAN_PROVIDER`. With `IMAGE_SCAN_PROVIDER=auto`, Davos chooses Gemini image scan. OpenAI is not used by auto routes; it is legacy explicit only if configured.
- Stale image env values such as `gemini-2.5-flash-image` are treated as legacy and fall back to `gemini-3.1-flash-image`.
- Nano Banana is an explicit separate Gemini image lane via `NANO_BANANA_IMAGE_MODEL`, currently `gemini-3.1-flash-image`, with `NANO_BANANA_IMAGE_SIZE=2K`.
- If asked for model "power rankings," explain the current stack in this order: local Ollama Gemma3 for normal/simple chat; local Flux for cheap/private image generation; Gemini 3.1 Flash-Lite for fallback/tool/rewrite work; Gemini 3.5 Flash for rare owner-only pro thinking/code review; Gemini 3.1 Flash Image for image scan and cloud image fallback; Nano Banana Gemini 3.1 Flash Image at 2K only when explicitly requested; Gemma4 is parked for later reliability work; OpenAI/GPT is not used by default routes.

**Log intent rules:**
- `log` (bare) → **display** the log
- `log [text]` → **write** text as a new entry
- Casual mention of "log" in a sentence → passes to LLM, not intercepted

**Safe cleanup / Codex handoff rules:**
- `ship safe cleanup` is a planning and handoff command only. It reads the
  current `change_log`, classifies items GREEN/YELLOW/RED, and returns a
  Codex-ready prompt that can be pasted into Windows or phone Codex.
- It does not edit files, run tests, commit, push, deploy, restart PM2, mutate
  cross-chat DB state, change model/runtime config, or mark items done.
- It can include a post-validation `log done #...` line. Use that only after
  Codex reports the rows were fixed, deployed, and smoked. Prefer `log done`
  over `log clear`; keep RED rows visible until an isolated review closes them.
- `big change`, `codex plan`, and `intake` create structured review-only rows
  in `change_log`. They are for owner/admin planning, not automatic execution.
- `fix yourself`, `self review`, `self diagnose`, `diagnose yourself`,
  `debug yourself`, `analyze this and log`, `log this and fix it`, typo'd
  analyze/log/fix variants, and `ship this cron fix` create structured
  review-only repair rows. They classify the likely failure category, mark
  sensitive repairs RED, capture image-scan/log/DB context metadata, explain
  the first diagnosis, and hand Codex a concrete repair path with
  Mini/validation notes.
- "Fix yourself" does **not** mean live self-edit or auto-deploy. The safe
  meaning is: capture owner feedback, diagnose, log a repair handoff, then let
  Windows Codex patch and Mini validate.
- Oversized owner requests are not dead ends. If a request is too large for one
  model call, log a durable guarded intake row with
  a redacted preview, message length, risk, expected behavior, and Codex-only
  branch/test/push/CI/deploy pipeline.
- The correct workflow is: phone logs messy idea -> `ship safe cleanup` creates
  a copy-paste prompt -> Codex pulls the live board over SSH, patches manually,
  validates, pushes, and smokes Mini runtime -> `log done #id #id` removes only
  completed rows.
- `log export` is the SSH bridge for the live phone board. It writes a private,
  gitignored Markdown snapshot locally on the Mac Mini. It does not commit or
  push runtime state to GitHub.

### Features

| Command | Access | What it does | Example |
|---|---|---|---|
| `bets` | Admin+ | List your open bets | `bets` |
| `bets new [opponent] [amount] [desc]` | Admin+ | Create a social bet | `bets new <phone> 50 who wins the fight` |
| `bets settle [id] [winner]` | Creator/Admin | Settle a social bet | `bets settle 3 <phone>` |
| `/bet log [event] [odds] [stake]u` | Everyone | Log a sports bet (units-based tracker) | `/bet log Lakers -110 2u` |
| `/bet settle [win/loss/push]` | Everyone | Settle a pending bet | `/bet settle win` |
| `/bet stats` | Everyone | Your bet stats this week (P&L, win rate, ROI) | `/bet stats` |
| `workout` | Owner | Show today's logged workout entries | `workout` |
| `/workout log [exercise] [sets]` | Owner | Log a workout set | `/workout log bench 185x5x3` |
| `/workout summary` | Owner | Weekly volume + top lifts + progression | `/workout summary` |
| `/workout plan` | Owner | AI recommendation for today's session | `/workout plan` |
| `sharecontact [email]` | Admin+ | Email Davos.vcf contact card via SMTP | `sharecontact <email>` |
| `scan [filename]` | Owner | Gemini code review of a project file | `scan tools.py` |
| `scheduled` | Owner | List pending scheduled iMessages | `scheduled` |
| `cancel [id]` | Owner | Cancel a pending scheduled task | `cancel 5` |
| `skills` | Admin+ | List all registered skills with status | `skills` |
| `skill enable [name]` | Admin+ | Enable a skill | `skill enable greeting` |
| `skill disable [name]` | Admin+ | Disable a skill | `skill disable greeting` |

### Group Chat Commands

| Command | Location | Access | What it does |
|---|---|---|---|
| `chats` | DM | Owner | List all enabled GCs with names, personas, IDs, and routing audit status |
| `chats stale` | DM | Owner | Preview stale group-chat routing warnings from `gc_state.json` vs `chat.db` |
| `chats disable stale confirm` | DM | Owner | Disable only stale enabled GC IDs after explicit confirmation |
| `@Davos on` | GC | Owner | Enable bot in this group chat |
| `@Davos off` | GC | Owner | Disable bot in this group chat |
| `@Davos allow [+number]` | GC | Owner | Grant friend-tier access to a number in any GC |
| `@Davos revoke [+number]` | GC | Owner | Remove friend-tier access |
| `@Davos persona [name]` | GC | Owner | Switch this GC's active persona |
| `@Davos persona reset` | GC | Owner | Return GC to default persona |
| `@Davos help` | GC | Open | Show help (tier-aware) |

### Help

| Command | Access | What it does |
|---|---|---|
| `help` | **Open** | Show command list (full for owner/admin, simplified for friends) |

---

## LLM Tools

The LLM tool set is exposed through Gemini function calling. Owner gets the full set; admins get `web_search` only; friends get no tools.

| Tool | Owner-only | Purpose |
|---|---|---|
| `web_search` | no | Current events, scores, odds, news (Tavily; "no web search" disables) |
| `read_file` / `write_file` | yes | Mac Mini file I/O |
| `shell_exec` | yes | Run shell commands |
| `sqlite_query` | yes | Arbitrary SQLite queries |
| `edit_persona` / `create_persona` | yes | Modify or create persona files |
| `generate_file` | yes | Create CSV/TXT and send via iMessage |
| `log_workout` / `query_workout` | no | Workout tracking |
| `log_change_request` | yes | Append to change log (batch from numbered lists) |
| `set_reminder` | no | Schedule a one-off reminder. **No `chat_id` parameter** — routing is fixed to `originating_chat_id`. Hidden DB id; user sees positional list. |
| `list_reminders` / `cancel_reminder` | no | Manage reminders. `cancel_reminder` takes `position` (1-based), not internal id. |
| `schedule_cron` | yes | Daily recurring message in current chat. Args: `time_pt` (HH:MM Pacific), optional `intro` line. Routing scoped to `originating_chat_id`. |
| `list_crons` / `cancel_cron` | yes | Manage daily jobs in the current chat. `cancel_cron` takes a position. |
| `get_group_chat_status` / `list_chats` / `clear_chat_history` | yes | Chat management |
| `send_imessage` | yes | Prepare private 1:1 confirmation only. Never sends immediately; admin password reply is required before send/schedule. |
| `get_inspirational_quote` | (called internally) | Used by `morning_message` cron jobs |

---

## Scheduled Tasks & Cron

Two distinct subsystems — don't confuse them:

**`scheduled_tasks` table** (one-off iMessages):
- Schema: `id, task_type, recipient, message, scheduled_at (UTC), status (pending/done/failed), chat_id, sender, error`
- Inserted only after a pending private `send_imessage` confirmation is approved with the admin password
- Background loop in `_check_scheduled_tasks` fires once at the target UTC time
- `chat_id` column: if set, routes to that GC instead of `recipient` (so GC-originated jobs don't fire to DM)

**`cron_jobs` table** (daily recurring):
- Schema: `id, cron_expression (HH:MM Pacific), action_type, action_payload (JSON), enabled, last_run, created_by`
- Inserted by `schedule_cron` LLM tool
- Background loop in `_check_cron_jobs` ticks every minute (UTC dedup against `last_run`); fires when current PT `HH:MM` matches `cron_expression`
- `action_type=morning_message`: generates a fresh Gemini quote per fire, optionally prepended with `payload.intro` (e.g. "good morning boys!")
- TZ: stdlib `zoneinfo.ZoneInfo("America/Los_Angeles")` — DST-aware. Do NOT add `pytz` dep.
- Routing: `payload.recipient` is the originating chat (32-hex GC GUID → group send; phone/email → DM)
- Visibility: `list_crons` / `cancel_cron` are scoped to the originating chat — same job is invisible from other chats

---

## DB Tables (davosbot.db)

| Table | Purpose |
|---|---|
| `messages` | Full conversation history |
| `workouts` | Workout log |
| `reminders` | Scheduled reminders (`origin_chat_id` for GC routing) |
| `tool_usage` | Per-sender tool counts (search/image limits) |
| `gemini_usage` | Token usage + cost by call |
| `missing_capabilities` | Capability-gap log |
| `bot_log` | Structured events (`event_type`, `payload` columns) |
| `bot_sessions` | Startup/heartbeat tracking |
| `admins` | Active admin records (with `revoked_at`) |
| `admin_audit` | Grant/revoke + persona-switch audit trail |
| `rate_limit_log` | Hourly per-sender count (1h window, max 20) |
| `scheduled_tasks` | Owner's scheduled iMessages |
| `change_log` | Pending owner change requests |
| `user_facts` | Extracted self-description facts |
| `bets` | Social/head-to-head bet tracker |
| `sports_bets` | Units-based sports bet tracker (open to everyone via `/bet`) |
| `bet_config` | Per-user bet tracker config (unit size, etc) |
| `workout_entries` | Per-set workout log |
| `workout_config` | Per-user workout tracker config |
| `skills` | Registered LLM skills (enable/disable) |
| `cron_jobs` | Recurring cron-style jobs (TZ: America/Los_Angeles via stdlib `zoneinfo`) |

---

## Intent Classification (brain.py)

Two intents are classified **before** the LLM is called:

**Reminder mentions:**
- `remind me` / `set a reminder` / `add a reminder` → scheduling intent → passes to LLM with `set_reminder` tool
- `cancel reminder` / `delete reminder` → cancel intent → passes to LLM with cancel tools
- Casual: "the reminder didn't work", "you missed the reminder" → intercepted, returns: *"Looks like the reminder didn't go through — want me to set it again?"* — does NOT touch any reminder in the DB

**Log writes:**
- `log` (bare) → display the change log
- `log [text]` → write `text` as a new change log entry
- Casual mention of "log" in a sentence → not intercepted, passes to LLM

---

## Known Gaps

- **iMessage file sending over SSH is unreliable** — AppleScript requires Messages.app focus. Use `sharecontact [email]` (SMTP-based) instead of trying to attach files via AppleScript.
- **Group chats must be created from `<email>`** — Apple doesn't normalize handles across Apple IDs. Chats created from the old phone number (`<phone>`) won't route correctly. `audit_group_chats()` flags stale GCs at startup (GC STALE warning in logs).
- **`persona [name]` is canonical; `switch to [name]` also works** — both paths call `_cmd_persona()`. The NL intent classifier handles all variants (`be`, `activate`, `go full`, etc.), but `persona [name]` is slightly more reliable.
- **Log intent requires explicit `log [text]` syntax** — saying "we should log this" in a sentence does NOT write to the log (passed to LLM). Only `log [text]` as the entire message writes. Bare `log` shows the log.

## Reminder routing — invariants

The reminder pipeline is the single most-broken-historically feature. These invariants prevent regression:

1. **Schema**: `reminders.origin_chat_id` MUST exist. `init_db` ALTERs in the column on existing DBs; new tables include it. Without this, `_set_reminder` INSERT and `_check_reminders` SELECT both crash silently.
2. **Routing context**: every LLM call that can fire `set_reminder` MUST pass `originating_chat_id=<sender or chat_id>` to `get_response`. The owner-DM path was the historical miss (main.py:349). Group-chat path passes the GC hex. Friend/admin DM paths don't have set_reminder access, so they don't need it.
3. **LLM placeholder defense**: `_set_reminder`'s `originating_chat_id` short-circuits the LLM-provided `chat_id`. `chat_id` is also no longer in the tool schema's `required` array — the executor uses `args.get("chat_id", "")` so omission is safe. Placeholder regex catches "default user", "current chat id", "sender", "owner" and similar hallucinations.
4. **Send target**: `_check_reminders` uses `origin_chat_id or chat_id` and detects 32-char hex → `is_group=True`. Anything else routes as DM with `is_group=False`.
5. **Date hallucination defense**: `personality.build_system_prompt` injects current UTC + Pacific timestamps with the explicit instruction "never use a year other than {now.year} unless the user names it." Without this, Gemini defaults to ~2024 dates from its training cutoff.

## Memory poisoning defense

`extract_and_update_memory` is gated behind `is_owner(sender)` in `handle_group_message`. Friends and admins talking in a GC CANNOT mutate `MEMORY.md` — only the owner's own messages can. This was the vector that previously seeded MEMORY with "Phone number 8025571835 has been granted approved user access" via test ingestion.

## Timezones

All timezone math uses stdlib `zoneinfo.ZoneInfo("America/Los_Angeles")`. Do NOT add `pytz` as a dependency — it's not in the venv and was the cause of `_check_cron_jobs error: No module named 'pytz'` earlier. `zoneinfo` is DST-aware via tzdata bundled with macOS Python.

---

## Dev Workflow

- Code edited in the local CLI; pushed to GitHub.
- Owner texts `pull` → bot self-deploys.
- DB backed up before every schema migration via `run_migration` in `db.py`.
- SOUL.md backed up before every write via `write_soul` in `soul.py`.

## .env (NEVER commit)

```
GEMINI_API_KEY=
TAVILY_API_KEY=
OWNER_ID=
MAC_MINI_APPLE_ID=<email>
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4
OLLAMA_SIMPLE_CHAT_MODEL=gemma3
GEMINI_MODEL=gemini-3.1-flash-lite
ADVANCED_TEXT_MODEL=gemini-3.5-flash
GEMINI_IMAGE_MODEL=gemini-3.1-flash-image
NANO_BANANA_IMAGE_MODEL=gemini-3.1-flash-image
NANO_BANANA_IMAGE_SIZE=2K
POLL_INTERVAL=5
DB_PATH=/Users/.../Library/Messages/chat.db
SMTP_HOST=
SMTP_PORT=465
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM_ADDRESS=
```

Note: `OLLAMA_MODEL=gemma4` can stay installed/configured for keep-warm/default
local checks, but live simple chat should use `OLLAMA_SIMPLE_CHAT_MODEL=gemma3`
until Gemma4 normal-chat reliability is proven. That does not affect Gemini
3.1/3.5 routing.
