# Fairgrounds Morning Briefing

Scheduled-agent runbook that generates a daily operational briefing for Fairgrounds site managers.

Each morning (7:30 AM ET) a Claude Code scheduled trigger fetches the day's sessions, events, and cancellations from PodPlay, computes today's and the next 14 days' utilization per site, uploads a heatmap CSV to Google Drive, and posts a per-location summary into Slack.

## Architecture

- **`prep.py`** — fully deterministic pipeline. Fetches PodPlay data via `$API_KEY`, computes utilization, uploads the heatmap sheet via the attached Google Drive MCP, and writes fully-rendered Slack messages to `/tmp/messages.json`. The scheduled agent then only has to post each message via curl.

The agent is intentionally minimal: it runs the script and posts what the script produced. If the pipeline fails, the agent reads `/tmp/briefing_status.json` and surfaces the error.

## Environment

The script expects to run inside a Claude Code scheduled trigger sandbox with:

- `API_KEY` — PodPlay JWT
- `/tmp/mcp-config-*.json` — MCP server URLs for `Slack` and `Google-Drive`
- `/home/claude/.claude/remote/.session_ingress_token` — MCP bearer token
- stdlib Python 3.11+

Optional env vars:

- `MODE=test` (default) — post to the B28 test channels only. `MODE=prod` — post to all Fairgrounds site channels.
- `DRY_RUN=1` — skip the Drive upload and use a placeholder URL.

## Output files

- `/tmp/briefing_output.json` — per-channel diagnostic data
- `/tmp/messages.json` — `{channel_id: {"name", "label", "body"}}` ready for Slack
- `/tmp/briefing_status.json` — `{"ok": bool, "errors": [...], "sheet_url": str|null}`
- `/tmp/heatmap.csv` — local copy of the uploaded CSV for debugging

## Updating

Push to `main`. The scheduled trigger fetches `prep.py` fresh each run, so changes take effect on the next cron firing (or the next manual `run`).
