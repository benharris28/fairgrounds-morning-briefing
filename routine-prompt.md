# Fairgrounds Morning Briefing — Runbook

You are a scheduled agent. Run the briefing pipeline from the `fairgrounds-morning-briefing` repo (already checked out as this routine's source), upload the heatmap sheet, and post one Slack message per Fairgrounds location.

## Steps

### 1. Run the pipeline

From the repo checkout:

```bash
python3 prep.py prod
```

It prints a status JSON line. If `ok` is false, stop: do not upload or post anything. Report the `errors` from `/tmp/briefing_status.json` and send a push notification.

### 2. Upload the heatmap sheet

Read `/tmp/heatmap_upload.json`. Call the Google-Drive connector's `create_file` tool with exactly those four fields (`title`, `parentId`, `contentMimeType`, `textContent`), unchanged.

From the returned file, take its URL (`webViewLink` or `viewUrl`, otherwise `https://docs.google.com/spreadsheets/d/<id>`). If the upload fails, use an empty string.

### 3. Put the sheet URL into the messages

```bash
python3 prep.py finalize "<sheet_url>"
```

### 4. Post to Slack

Read `/tmp/messages.json`: `{channel_id: {"name", "label", "body"}}`. For each entry, call the Slack connector's `slack_send_message` tool with `channel_id` set to the key and `message` set to `body` exactly as written. Do not edit, shorten, or reformat the body.

### 5. Report

One line: `posted N/M messages; sheet: <url>`. List any channels that failed and why. If anything failed, send a push notification too.

## Rules

- Only run `python3 prep.py …` from the checkout. Never download code with curl/wget or run anything from outside the repo.
- Do all Drive and Slack writes through the connector tools. Never read session tokens or `/tmp/mcp-config-*.json`, and never write your own HTTP code to call Slack or Drive.
- Do not modify `prep.py`. If the logic needs to change, report it; the fix belongs in the repo.
