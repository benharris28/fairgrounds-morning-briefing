# Fairgrounds Morning Briefing

Scheduled-agent runbook that generates a daily operational briefing for Fairgrounds site managers.

Every morning at **7:30 AM ET**, a Claude Code scheduled trigger fetches the day's sessions, events, and cancellations from PodPlay, computes today's and the next 14 days' utilization per site, uploads a heatmap CSV to Google Drive, and posts a per-location summary to Slack.

## What each briefing contains

- A headline utilization % for today (Leaside breaks out Indoor Padel / Outdoor Padel / Pickle + Total)
- Count of programmed events and private court bookings
- Link to today's 14-day forward heatmap sheet
- Today's events grouped: Open Play, Leagues, Clinics & Lessons, Private Events
- Cancellations (only if there are any)

## Architecture

The design splits into a deterministic Python pipeline + a tiny LLM agent.

```
┌─────────────────────────────────────────────────────────────┐
│ Claude Code scheduled trigger (env_01QtXL9hzpTgWzS3sunQz1KY) │
│  cron · this repo attached as the routine's source            │
│  ├─ python3 prep.py prod           (from the checkout)        │
│  │    ├─ GET /areas                                           │
│  │    ├─ GET /sessions (today)    ─┐                          │
│  │    ├─ GET /events (today)       ├─ parallel                │
│  │    ├─ GET /sessions (+14 days)  ─┘ (chunked to 7-day max)  │
│  │    ├─ GET /events/{id}/signups  (top-10 by signups)        │
│  │    ├─ compute utilization                                  │
│  │    ├─ build CSV                                            │
│  │    ├─ write /tmp/heatmap_upload.json                       │
│  │    └─ render Slack messages → /tmp/messages.json           │
│  ├─ agent: Google-Drive create_file (heatmap_upload.json)     │
│  ├─ python3 prep.py finalize <sheet_url>                      │
│  └─ agent: Slack slack_send_message per messages.json entry   │
└─────────────────────────────────────────────────────────────┘
```

**Why this split?** Early versions let the LLM generate the fetch/compute/upload code fresh each run. That wastes tokens, hits streaming timeouts, and makes every run non-deterministic. Moving the pipeline into a versioned Python script gives us repeatability, and keeps the LLM's role to what it's actually good at (error recovery + final message dispatch).

### Files

- `prep.py` — the whole pipeline. Stdlib-only Python (no pip install needed in the trigger sandbox). Fetches data, computes utilization, builds the heatmap CSV, renders Slack message bodies. It never calls MCP servers or reads session credentials — the agent does all Drive and Slack writes through its own connectors.
- `routine-prompt.md` — the exact prompt the routine runs. Keep it in sync with the RemoteTrigger config.
- `.claude/settings.json` — pre-approves the pipeline's commands and the two connector tools it uses, so the unattended run isn't waiting on a permission decision.

### Why it's built this way (auto mode)

Routines run in auto mode, where a safety classifier reviews each action and a saved prompt doesn't count as live user approval. Two things in the original design were blocked every morning from at least early September 2026:

1. **`curl … prep.py && python3 prep.py`** — downloading code and running it straight away is blocked as "Code from External". The repo is now attached as the routine's source, so the code is already checked out.
2. **Reading `.session_ingress_token` and calling the Slack/Drive MCP URLs by hand.** That's credential use behind the harness's back. The agent now calls `create_file` and `slack_send_message` as normal connector tools.

Don't reintroduce either pattern.

## Utilization math

For each `(pod, local_day)`:

```
booked_court_hours   = Σ (court_count - tablesLeft) × slot_duration_hours
capacity_court_hours = Σ court_count × slot_duration_hours
utilization          = booked / capacity
```

The sum runs over every session the PodPlay `/sessions` API returned for that pod on that day. The API only returns bookable slots within each pod's operating hours, so the denominator is the exact set of court-hours that could have been booked — no fixed-hour assumption.

Aggregation:

- **Non-Leaside sites:** sum booked + capacity across all court pods on the date, then divide.
- **Leaside:** compute per pod category (`indoor_padel`, `outdoor_padel`, `pickle`) + a `total` that sums across all Leaside court pods.

Clamped to `[0.0, 1.0]`.

Color thresholds for the heatmap CSV:

- 🟢 ≥ 65% (on pace)
- 🟡 45–64% (soft)
- 🔴 < 45% (push promo)

### Known gotchas

- **`tables.items` includes NOT_AVAILABLE tables.** Leaside Pickleball reports 13 entries but only 11 are bookable. Count `status == "AVAILABLE"` only.
- **PodPlay's WAF blocks `Python-urllib/X.Y`.** Always send a real `User-Agent` header.
- **`/sessions` returns 500 for windows > 7 days.** The script chunks wider windows automatically.

## Environment

The script expects to run inside a Claude Code scheduled trigger sandbox with:

- `$API_KEY` — PodPlay JWT (set on the routine's environment)
- Slack + Google-Drive connectors attached to the routine
- Python 3.11+ (stdlib only)

Mode is the first argument: `python3 prep.py test` (B28 test channels) or `python3 prep.py prod` (all Fairgrounds site channels). `$MODE` is used if no argument is given.

## Channel mappings

**Test mode (B28 workspace):** fg-kingston, fg-cloverdale, fg-leaside, fg-whitby.

**Prod mode (Fairgrounds workspace):** all 12 active sites. Both maps live in `prep.py` under `CHANNEL_MAP_TEST` / `CHANNEL_MAP_PROD`.

## Output files (written to `/tmp/`)

- `briefing_output.json` — full diagnostic dump (per-channel data, utilization, counts)
- `messages.json` — `{channel_id: {"name", "label", "body"}}` — exactly what gets posted
- `briefing_status.json` — `{"ok": bool, "errors": [...], "sheet_url": str|null}`
- `heatmap_upload.json` — `{title, parentId, contentMimeType, textContent}`, passed as-is to Drive `create_file`
- `heatmap.csv` — local copy of the uploaded CSV for debugging

## Operations

### Updating the logic

Push to `main`. The routine checks out the repo fresh on every run, so changes go live at the next cron firing (or next manual `RemoteTrigger run`).

### Running manually

From your local machine (needs `$API_KEY` from the Fairgrounds `.env`):

```bash
cd Projects/Fairgrounds
set -a && source .env && set +a
python3 ../fairgrounds-morning-briefing/prep.py test
cat /tmp/briefing_status.json
```

This only fetches and renders — nothing is uploaded or posted. Message bodies contain `__HEATMAP_URL__` until you run `prep.py finalize <url>`.

### Triggering a real run

```
RemoteTrigger action=run trigger_id=trig_018iWCX8x1GDCLhYPJv8Yot8
```

Or let the 7:30 AM ET cron fire.

### Debugging a failed run

1. `RemoteTrigger action=list_runs` then `get_run_log` on the run. Look for `permission_denied` lines first — a classifier block looks like a success in the run list.
2. If the script errored during fetch: re-run manually from your laptop against live PodPlay data to reproduce.
3. If the agent errored after `prep.py` succeeded: the `messages.json` + `briefing_output.json` in the sandbox are probably fine; the issue is in the posting loop (check Slack MCP response).

### Trigger configuration

- **Trigger ID:** `trig_018iWCX8x1GDCLhYPJv8Yot8`
- **Name:** Morning Briefing Bot
- **Cron:** `30 9 * * *` (UTC)
- **Environment:** `env_01QtXL9hzpTgWzS3sunQz1KY`
- **Source repo:** `benharris28/fairgrounds-morning-briefing`
- **Model:** `claude-opus-5-5`
- **MCP connectors:** Slack + Google-Drive
- **Allowed tools:** Bash, Read, Glob, Grep, ToolSearch
- **Prompt:** `routine-prompt.md`

## Heatmap sheet archive

Daily sheets land in: **[Fairgrounds > Automations > Heatmaps](https://drive.google.com/drive/folders/1S_Cn6mgoKnMh00lBc78YsX-9wDfxYmTP)** (`1S_Cn6mgoKnMh00lBc78YsX-9wDfxYmTP`).

Folder ID is hardcoded in `prep.py` as `HEATMAP_FOLDER_ID`.
