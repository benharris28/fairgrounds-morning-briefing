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
│  7:30 AM ET cron                                              │
│  ├─ curl raw.githubusercontent.com/…/prep.py                  │
│  ├─ python3 /tmp/prep.py                                      │
│  │    ├─ GET /areas                                           │
│  │    ├─ GET /sessions (today)    ─┐                          │
│  │    ├─ GET /events (today)       ├─ parallel                │
│  │    ├─ GET /sessions (+14 days)  ─┘ (chunked to 7-day max)  │
│  │    ├─ GET /events/{id}/signups  (top-10 by signups)        │
│  │    ├─ compute utilization                                  │
│  │    ├─ build CSV                                            │
│  │    ├─ upload to Google Drive via Google-Drive MCP          │
│  │    └─ render Slack messages → /tmp/messages.json           │
│  └─ agent loops messages.json, posts each via Slack MCP       │
└─────────────────────────────────────────────────────────────┘
```

**Why this split?** Early versions let the LLM generate the fetch/compute/upload code fresh each run. That wastes tokens, hits streaming timeouts, and makes every run non-deterministic. Moving the pipeline into a versioned Python script gives us repeatability, and keeps the LLM's role to what it's actually good at (error recovery + final message dispatch).

### Files

- `prep.py` — the whole pipeline. Stdlib-only Python (no pip install needed in the trigger sandbox). Fetches data, computes utilization, uploads the heatmap sheet, renders Slack message bodies.
- The trigger prompt (in Claude Code's RemoteTrigger config, not in this repo) is ~60 lines: `curl prep.py → python3 prep.py → loop messages.json → post`.

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

- `$API_KEY` — PodPlay JWT
- `/tmp/mcp-config-*.json` — MCP server URLs for `Slack` and `Google-Drive`
- `/home/claude/.claude/remote/.session_ingress_token` — MCP bearer token
- Python 3.11+ (stdlib only)

Optional env vars:

- `MODE` — `test` (default) posts to the B28 test channels only. `prod` posts to all Fairgrounds site channels.
- `DRY_RUN` — any truthy value skips the Drive upload and uses a placeholder URL. Useful for local testing.

## Channel mappings

**Test mode (B28 workspace):** fg-kingston, fg-cloverdale, fg-leaside, fg-whitby.

**Prod mode (Fairgrounds workspace):** all 11 active sites. Both maps live in `prep.py` under `CHANNEL_MAP_TEST` / `CHANNEL_MAP_PROD`.

## Output files (written to `/tmp/`)

- `briefing_output.json` — full diagnostic dump (per-channel data, utilization, counts)
- `messages.json` — `{channel_id: {"name", "label", "body"}}` — exactly what gets posted
- `briefing_status.json` — `{"ok": bool, "errors": [...], "sheet_url": str|null}`
- `heatmap.csv` — local copy of the uploaded CSV for debugging

## Operations

### Updating the logic

Push to `main`. The scheduled trigger fetches `prep.py` fresh on every run, so changes go live at the next cron firing (or next manual `RemoteTrigger run`). Note: `raw.githubusercontent.com` caches for 5 minutes — updates may take that long to propagate.

### Running manually

From your local machine (needs `$API_KEY` from the Fairgrounds `.env`):

```bash
cd Projects/Fairgrounds
set -a && source .env && set +a
DRY_RUN=1 MODE=test python3 ../fairgrounds-morning-briefing/prep.py
cat /tmp/briefing_status.json
```

Without the MCP config (which only exists inside the trigger sandbox), the upload + post steps can't run from a laptop. For end-to-end testing outside the trigger, upload `/tmp/heatmap.csv` manually to the Drive folder and use a Slack client to post each message from `/tmp/messages.json`.

### Triggering a real run

```
RemoteTrigger action=run trigger_id=trig_018iWCX8x1GDCLhYPJv8Yot8
```

Or let the 7:30 AM ET cron fire.

### Debugging a failed run

1. Inspect the trigger's session output in claude.ai (look for `/tmp/briefing_status.json` echo at the end of the transcript).
2. If the script errored during fetch: re-run manually from your laptop against live PodPlay data to reproduce.
3. If the agent errored after `prep.py` succeeded: the `messages.json` + `briefing_output.json` in the sandbox are probably fine; the issue is in the posting loop (check Slack MCP response).

### Trigger configuration

- **Trigger ID:** `trig_018iWCX8x1GDCLhYPJv8Yot8`
- **Name:** Morning Briefing Bot
- **Cron:** `30 11 * * *` (7:30 AM ET)
- **Environment:** `env_01QtXL9hzpTgWzS3sunQz1KY`
- **Model:** `claude-sonnet-4-6`
- **MCP connectors:** Slack + Google-Drive
- **Allowed tools:** Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, ToolSearch

## Heatmap sheet archive

Daily sheets land in: **[Fairgrounds > Automations > Heatmaps](https://drive.google.com/drive/folders/1S_Cn6mgoKnMh00lBc78YsX-9wDfxYmTP)** (`1S_Cn6mgoKnMh00lBc78YsX-9wDfxYmTP`).

Folder ID is hardcoded in `prep.py` as `HEATMAP_FOLDER_ID`.
