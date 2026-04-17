#!/usr/bin/env python3
"""
Morning briefing prep for Fairgrounds.

Deterministic pipeline: fetch PodPlay data, compute utilization, build 14-day
heatmap CSV, upload to Google Drive, render Slack messages, write results to
/tmp/ for an LLM agent to post.

Runs inside the scheduled trigger environment. Dependencies: stdlib only.

Env vars read:
  API_KEY          — PodPlay JWT (required)
  MODE             — 'test' (only post to B28 test channels) or 'prod' (all channels).
                     Defaults to 'test'.
  DRY_RUN          — if set, skip the Drive upload and use a placeholder URL.

Output files:
  /tmp/briefing_output.json — full structured output (diagnostics + per-area data)
  /tmp/messages.json        — {channel_id: {"name": str, "body": str}} for posting
  /tmp/briefing_status.json — {"ok": bool, "errors": [...], "sheet_url": str|null}
"""
from __future__ import annotations

import base64
import csv
import glob
import http.client
import io
import json
import os
import random
import re
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PODPLAY_BASE = "https://fairgrounds.podplay.app/apis/v2"
HEATMAP_FOLDER_ID = "1S_Cn6mgoKnMh00lBc78YsX-9wDfxYmTP"
HEATMAP_FOLDER_URL = f"https://drive.google.com/drive/folders/{HEATMAP_FOLDER_ID}"


# Channel mappings (test vs prod). Match by substring (case-insensitive) against displayName.
CHANNEL_MAP_TEST = {
    "cataraqui":    {"channel_id": "C0AQV77J7UL", "channel_name": "fg-kingston",    "label": "Kingston"},
    "cloverdale":   {"channel_id": "C0ARE7C1J06", "channel_name": "fg-cloverdale",  "label": "Cloverdale"},
    "leaside":      {"channel_id": "C0ATFQGQ1UN", "channel_name": "fg-leaside",     "label": "Leaside"},
    "whitby":       {"channel_id": "C0ARG99KYUS", "channel_name": "fg-whitby",      "label": "Whitby"},
}

CHANNEL_MAP_PROD = {
    "barton":         {"channel_id": "C0A6TM6S9PS", "channel_name": "fg-hamilton",       "label": "Hamilton"},
    "base31":         {"channel_id": "C0A884F40MU", "channel_name": "fg-base31",         "label": "Base31"},
    "capilano":       {"channel_id": "C09SYG49DCM", "channel_name": "fg-capilano",       "label": "Capilano"},
    "cataraqui":      {"channel_id": "C0A1CAWUVBM", "channel_name": "fg-kingston",       "label": "Kingston"},
    "cloverdale":     {"channel_id": "C0AR9CFL5EH", "channel_name": "fg-cloverdale",     "label": "Cloverdale"},
    "leaside":        {"channel_id": "C0A86QCK5EZ", "channel_name": "fg-leaside",        "label": "Leaside"},
    "red deer":       {"channel_id": "C09T20ANYH0", "channel_name": "fg-red-deer",       "label": "Red Deer"},
    "rosehill":       {"channel_id": "C0ARA9876R4", "channel_name": "fg-rosehill",       "label": "Rosehill"},
    "stackt":         {"channel_id": "C0ARA9KB3N2", "channel_name": "fg-stackt",         "label": "Stackt"},
    "assembly park":  {"channel_id": "C0APRAKS7AM", "channel_name": "fg-assembly-park",  "label": "Assembly Park"},
    "whitby":         {"channel_id": "C09SLCJALQ7", "channel_name": "fg-whitby",         "label": "Whitby"},
}

# Heatmap CSV row order (stable labels; maps to area displayName substring)
HEATMAP_ROWS = [
    ("Leaside — Indoor Padel",  ("leaside", "indoor_padel")),
    ("Leaside — Outdoor Padel", ("leaside", "outdoor_padel")),
    ("Leaside — Pickle",        ("leaside", "pickle")),
    ("Leaside — Total",         ("leaside", "total")),
    ("Capilano",                ("capilano", "main")),
    ("Cloverdale",              ("cloverdale", "main")),
    ("Kingston",                ("cataraqui", "main")),
    ("Rosehill",                ("rosehill", "main")),
    ("Whitby",                  ("whitby", "main")),
    ("Hamilton",                ("barton", "main")),
    ("Red Deer",                ("red deer", "main")),
    ("Base31",                  ("base31", "main")),
    ("Stackt",                  ("stackt", "main")),
    ("Assembly Park",           ("assembly park", "main")),
]

HANGUL_FILLER = "\u3164"  # invisible line for Slack vertical spacing


# ----------------------------- HTTP helpers -----------------------------

USER_AGENT = "fairgrounds-morning-briefing/1.0 (+https://github.com/benharris28/fairgrounds-morning-briefing)"


def podplay_get(path: str, timeout: int = 90, max_retries: int = 5) -> dict:
    """GET with exponential backoff on transient failures (IncompleteRead,
    connection resets, timeouts, 5xx, 429). Schedule: ~5s, 15s, 45s, 90s, 90s
    (with 0–30% jitter) — roughly a 4-minute budget, enough to ride out a
    short PodPlay WAF/rate-limit blip or CDN edge wobble."""
    key = os.environ["API_KEY"]
    url = f"{PODPLAY_BASE}{path}"
    headers = {
        "x-api-key": key,
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Retry only 5xx + 429; re-raise 4xx immediately (auth/bad request)
            if e.code < 500 and e.code != 429:
                raise
            last_exc = e
        except (http.client.IncompleteRead, urllib.error.URLError,
                socket.timeout, TimeoutError, ConnectionError) as e:
            last_exc = e
        if attempt == max_retries:
            break
        base = min(90, 5 * (3 ** attempt))  # 5, 15, 45, 90, 90
        backoff = base + random.uniform(0, base * 0.3)
        print(f"[podplay_get] {path} attempt {attempt + 1} failed "
              f"({type(last_exc).__name__}: {last_exc}); retrying in {backoff:.1f}s",
              file=sys.stderr)
        time.sleep(backoff)
    assert last_exc is not None
    raise last_exc


def mcp_discover() -> tuple[str, str, str]:
    """Return (slack_url, drive_url, bearer_token)."""
    cfg_paths = glob.glob("/tmp/mcp-config-*.json")
    if not cfg_paths:
        raise RuntimeError("No /tmp/mcp-config-*.json found")
    with open(cfg_paths[0]) as f:
        cfg = json.load(f)
    servers = cfg.get("mcpServers", {})
    slack_url = servers.get("Slack", {}).get("url", "")
    drive_url = servers.get("Google-Drive", {}).get("url", "")

    token_path = "/home/claude/.claude/remote/.session_ingress_token"
    token = ""
    if os.path.exists(token_path):
        token = open(token_path).read().strip()
    else:
        try:
            token = os.read(4, 4096).decode().strip()
        except Exception:
            pass
    if not token:
        raise RuntimeError("Could not read ingress token")
    return slack_url, drive_url, token


MCP_HEADERS_BASE = {
    "Content-Type": "application/json",
    # Streamable HTTP transport requires both; server picks one in Content-Type.
    "Accept": "application/json, text/event-stream",
}


def _mcp_parse_response(raw: bytes, content_type: str) -> dict:
    """MCP Streamable HTTP can reply with application/json OR text/event-stream.
    In SSE the body is a sequence of `data: <json>\\n\\n` frames; we want the
    last `data:` line (the final JSON-RPC response)."""
    ct = (content_type or "").lower()
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in ct or text.lstrip().startswith("event:") or text.lstrip().startswith("data:"):
        last_data = None
        for line in text.splitlines():
            if line.startswith("data:"):
                last_data = line[5:].strip()
        if last_data is None:
            raise ValueError(f"SSE response had no data frames: {text[:200]!r}")
        return json.loads(last_data)
    return json.loads(text)


def mcp_list_tools(url: str, token: str, timeout: int = 30) -> list[str]:
    """Return the tool names exposed by an MCP server at URL."""
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode()
    req = urllib.request.Request(url, data=body, headers={
        **MCP_HEADERS_BASE,
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        resp = _mcp_parse_response(r.read(), r.headers.get("Content-Type", ""))
    tools = (resp.get("result") or {}).get("tools", []) or []
    return [t.get("name") for t in tools if t.get("name")]


def mcp_call(url: str, token: str, tool_name: str, args: dict, timeout: int = 60) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "method": "tools/call", "id": 1,
        "params": {"name": tool_name, "arguments": args},
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        **MCP_HEADERS_BASE,
        "Authorization": f"Bearer {token}",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _mcp_parse_response(r.read(), r.headers.get("Content-Type", ""))


# ----------------------------- Areas + pods -----------------------------

def fetch_areas() -> dict:
    """Fetch /areas and build filtered lookup structures."""
    data = podplay_get("/areas")
    items = data.get("items", []) or []

    areas_out = []
    pod_to_area = {}
    pod_capacity = {}
    pod_category = {}
    pod_timezone = {}
    area_timezone = {}
    area_name = {}  # area_id -> lowercase displayName

    for area in items:
        if area.get("status") != "AVAILABLE":
            continue
        display = (area.get("displayName") or "").strip()
        dlo = display.lower()
        if any(k in dlo for k in ("closed", "don't buy", "dont buy")):
            continue

        pods = (area.get("pods") or {}).get("items", []) or []
        kept = []
        tz_fallback = None
        for pod in pods:
            if pod.get("status") != "AVAILABLE":
                continue
            pd = ((pod.get("displayName") or "") + " " + (pod.get("description") or "")).lower()
            if "court" not in pd:
                continue
            tables = (pod.get("tables") or {}).get("items", []) or []
            # Count only AVAILABLE tables — NOT_AVAILABLE tables still appear in the list
            # but aren't bookable (e.g., Leaside Pickleball has 13 entries but only 11 are live).
            cap = sum(1 for t in tables if t.get("status") == "AVAILABLE")
            if cap == 0:
                continue
            pod_id = pod["id"]
            pod_to_area[pod_id] = area["id"]
            pod_capacity[pod_id] = cap
            pod_timezone[pod_id] = pod.get("timezone") or "America/Toronto"
            tz_fallback = tz_fallback or pod_timezone[pod_id]

            # Leaside pod categorization
            if "leaside" in dlo:
                if "padel" in pd and "indoor" in pd:
                    cat = "indoor_padel"
                elif "padel" in pd and "outdoor" in pd:
                    cat = "outdoor_padel"
                elif "pickle" in pd:
                    cat = "pickle"
                else:
                    cat = "main"
            else:
                cat = "main"
            pod_category[pod_id] = cat

            kept.append({
                "id": pod_id,
                "displayName": pod.get("displayName"),
                "description": pod.get("description"),
                "tables": cap,
                "category": cat,
                "timezone": pod_timezone[pod_id],
            })

        if not kept:
            continue
        area_timezone[area["id"]] = tz_fallback
        area_name[area["id"]] = dlo
        areas_out.append({
            "id": area["id"],
            "displayName": display,
            "timezone": tz_fallback,
            "pods": kept,
        })

    return {
        "areas": areas_out,
        "pod_to_area": pod_to_area,
        "pod_capacity": pod_capacity,
        "pod_category": pod_category,
        "pod_timezone": pod_timezone,
        "area_timezone": area_timezone,
        "area_name": area_name,
    }


# ----------------------------- Sessions + events -----------------------------

MAX_SESSION_WINDOW_DAYS = 7  # PodPlay returns 500/503 for wider ranges


def fetch_sessions(start: date, end: date) -> list:
    """Fetch /sessions; auto-chunks windows larger than 7 days (PodPlay limit)."""
    total_days = (end - start).days
    if total_days <= MAX_SESSION_WINDOW_DAYS:
        path = f"/sessions?startTime={start.isoformat()}T04:00:00Z&endTime={end.isoformat()}T07:00:00Z"
        data = podplay_get(path)
        return data.get("items", []) or []

    # Chunk into overlapping half-open ranges
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + timedelta(days=MAX_SESSION_WINDOW_DAYS))
        chunks.append((cursor, chunk_end))
        cursor = chunk_end

    items: list = []
    seen_ids: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(4, len(chunks))) as ex:
        futures = [ex.submit(fetch_sessions, s, e) for (s, e) in chunks]
        for fut in futures:
            for s in fut.result():
                sid = s.get("id") or f"{(s.get('pod') or {}).get('id')}@{s.get('startTime')}"
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                items.append(s)
    return items


def fetch_events(start: date, end: date) -> list:
    path = f"/events?startTime={start.isoformat()}T04:00:00Z&endTime={end.isoformat()}T07:00:00Z"
    data = podplay_get(path)
    return data.get("items", []) or []


def fetch_event_signups(event_id: str) -> list:
    path = f"/events/{event_id}/signups?includeCanceled=true"
    data = podplay_get(path)
    return data.get("items", []) or []


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


# ----------------------------- Utilization -----------------------------

def compute_utilization(sessions: list, info: dict) -> dict:
    """Returns {area_id: {date_str: util}} where util is a float OR,
    for Leaside, a dict {"total": f, "indoor_padel": f, "outdoor_padel": f, "pickle": f}.

    Definition of capacity: the sum of `court_count × slot_duration_hours` across every
    session the PodPlay `/sessions` API returned for that pod on that day. The API
    only returns bookable slots within each pod's operating hours, so this is the
    exact set of court-hours that could have been booked — no fixed-hour assumption
    needed. Utilization = booked court-hours / bookable court-hours.
    """
    pod_to_area = info["pod_to_area"]
    pod_capacity = info["pod_capacity"]
    pod_category = info["pod_category"]
    pod_timezone = info["pod_timezone"]
    area_name = info["area_name"]

    pod_day_booked: dict[tuple[str, str], float] = {}
    pod_day_capacity: dict[tuple[str, str], float] = {}

    for s in sessions:
        pod_obj = s.get("pod") or {}
        pod = pod_obj.get("id") if isinstance(pod_obj, dict) else None
        if not pod or pod not in pod_to_area:
            continue

        start = parse_iso(s.get("startTime"))
        if not start:
            continue
        end = parse_iso(s.get("endTime")) or (start + timedelta(minutes=30))
        duration_h = max(0.0, (end - start).total_seconds() / 3600.0)
        if duration_h <= 0:
            continue

        tz = ZoneInfo(pod_timezone[pod])
        day_str = start.astimezone(tz).date().isoformat()

        cap = pod_capacity[pod]
        tables_left = s.get("tablesLeft")
        if tables_left is None:
            tables_left = cap
        courts_booked = max(0, cap - int(tables_left))

        key = (pod, day_str)
        pod_day_booked[key] = pod_day_booked.get(key, 0.0) + courts_booked * duration_h
        pod_day_capacity[key] = pod_day_capacity.get(key, 0.0) + cap * duration_h

    # Group pods by area
    area_pods: dict[str, list[str]] = {}
    for pod, area_id in pod_to_area.items():
        area_pods.setdefault(area_id, []).append(pod)

    util: dict[str, dict[str, float | dict]] = {}
    for area_id, pods in area_pods.items():
        is_leaside = "leaside" in area_name.get(area_id, "")
        days_seen = {d for (p, d) in pod_day_capacity if p in pods}
        util[area_id] = {}
        for d in sorted(days_seen):
            if is_leaside:
                by_cat: dict[str, tuple[float, float]] = {}
                total_b, total_c = 0.0, 0.0
                for pod in pods:
                    b = pod_day_booked.get((pod, d), 0.0)
                    c = pod_day_capacity.get((pod, d), 0.0)
                    cat = pod_category[pod]
                    bb, cc = by_cat.get(cat, (0.0, 0.0))
                    by_cat[cat] = (bb + b, cc + c)
                    total_b += b
                    total_c += c
                breakdown: dict[str, float | None] = {
                    "total": None, "indoor_padel": None, "outdoor_padel": None, "pickle": None,
                }
                for cat, (b, c) in by_cat.items():
                    if c > 0 and cat in breakdown:
                        breakdown[cat] = clamp01(b / c)
                if total_c > 0:
                    breakdown["total"] = clamp01(total_b / total_c)
                util[area_id][d] = breakdown
            else:
                b, c = 0.0, 0.0
                for pod in pods:
                    b += pod_day_booked.get((pod, d), 0.0)
                    c += pod_day_capacity.get((pod, d), 0.0)
                util[area_id][d] = clamp01(b / c) if c > 0 else None

    return util


def clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


# ----------------------------- Event categorization -----------------------------

CATEGORIES_ORDER = ["Open Play", "Leagues", "Clinics & Lessons", "Private Events"]

# PodPlay tags every event with a subtype. Map it directly — do not rely on
# name keywords, which silently drop anything PodPlay introduces a new label
# for (e.g. KIDS_CLASS, TOURNAMENT).
SUBTYPE_TO_CATEGORY = {
    "open_play":     "Open Play",
    "league_night":  "Leagues",
    "tournament":    "Leagues",
    "adult_class":   "Clinics & Lessons",
    "kids_class":    "Clinics & Lessons",
    "coaching":      "Clinics & Lessons",
    "private_event": "Private Events",
    "party":         "Private Events",
    "private":       "Private Court Bookings",
    "operations":    "Operations",
}

# Name-keyword fallback for events with no subtype set (legacy safety net).
CLINIC_KEYWORDS = ["clinic", "lesson", "class", "cardio", "coach", "foundations",
                   "101", "learn to play", "prep", "tactics", "positioning"]
OPENPLAY_KEYWORDS = ["open play", "liveball", "live ball"]
LEAGUE_KEYWORDS = ["league", "ladder", "dupr"]


def categorize_event(event: dict) -> str:
    subtype = (event.get("subtype") or "").lower()
    if subtype in SUBTYPE_TO_CATEGORY:
        return SUBTYPE_TO_CATEGORY[subtype]

    name = (event.get("name") or "").lower()
    custom = (event.get("customType") or "").lower()
    if custom == "private event":
        return "Private Events"
    if custom == "private":
        return "Private Court Bookings"
    if custom == "open play" or any(k in name for k in OPENPLAY_KEYWORDS):
        return "Open Play"
    if any(k in name for k in LEAGUE_KEYWORDS):
        return "Leagues"
    if any(k in name for k in CLINIC_KEYWORDS):
        return "Clinics & Lessons"
    return "Other"


def normalize_event_name(n: str) -> str:
    return re.sub(r"\s+", " ", (n or "").strip().lower())


def categorize_events_by_area(events: list, info: dict) -> dict:
    """Returns {area_id: {category: [event_out...]}} + counts."""
    pod_to_area = info["pod_to_area"]
    seen = set()
    area_buckets: dict[str, dict[str, list]] = {}
    area_private_count: dict[str, int] = {}
    area_programmed_count: dict[str, int] = {}

    for ev in events:
        eid = ev.get("id")
        if eid in seen:
            continue
        seen.add(eid)

        # Determine area — first pod with a known area
        area_id = None
        pods = ((ev.get("pods") or {}).get("items")) or []
        for p in pods:
            a = (p.get("area") or {}).get("id") if isinstance(p.get("area"), dict) else None
            if a and a in info["area_timezone"]:
                area_id = a
                break
        if not area_id:
            continue

        cat = categorize_event(ev)
        if cat == "Operations":
            continue
        area_buckets.setdefault(area_id, {c: [] for c in CATEGORIES_ORDER + ["Other"]})

        if cat == "Private Court Bookings":
            area_private_count[area_id] = area_private_count.get(area_id, 0) + 1
            continue
        if cat in CATEGORIES_ORDER:
            area_programmed_count[area_id] = area_programmed_count.get(area_id, 0) + 1

        start = parse_iso(ev.get("startTime"))
        end = parse_iso(ev.get("endTime"))
        signups = ((ev.get("signups") or {}).get("_total")) or 0
        area_buckets[area_id].setdefault(cat, []).append({
            "id": eid,
            "name": (ev.get("name") or "").strip(),
            "norm_name": normalize_event_name(ev.get("name")),
            "start": start,
            "end": end,
            "signups": signups,
            "court_count": len(pods),
        })

    # Consolidate Private Events by (normalized name, start, end)
    for area_id, cats in area_buckets.items():
        privates = cats.get("Private Events", [])
        grouped: dict[tuple, dict] = {}
        for p in privates:
            k = (p["norm_name"], p["start"], p["end"])
            if k in grouped:
                grouped[k]["court_count"] += p["court_count"]
            else:
                grouped[k] = dict(p)
        cats["Private Events"] = sorted(grouped.values(),
                                        key=lambda x: (x["start"] or datetime.max.replace(tzinfo=timezone.utc)))
        # Sort other categories by start time too
        for c in CATEGORIES_ORDER:
            if c == "Private Events":
                continue
            cats[c].sort(key=lambda x: (x["start"] or datetime.max.replace(tzinfo=timezone.utc)))

    return {
        "area_buckets": area_buckets,
        "area_private_count": area_private_count,
        "area_programmed_count": area_programmed_count,
    }


# ----------------------------- Cancellations -----------------------------

def fetch_cancellations(events: list, top_n: int = 10, max_workers: int = 6) -> dict:
    """Returns {event_id: {"name": str, "area_id": str|None, "count": int,
                            "start": datetime, "end": datetime}}."""
    # Top N events by signup count
    ranked = sorted(
        events,
        key=lambda e: ((e.get("signups") or {}).get("_total") or 0),
        reverse=True,
    )[:top_n]

    out: dict[str, dict] = {}

    def work(ev):
        eid = ev.get("id")
        try:
            signups = fetch_event_signups(eid)
        except Exception:
            return None
        canceled = [s for s in signups if s.get("canceledAt")]
        if not canceled:
            return None
        pods = ((ev.get("pods") or {}).get("items")) or []
        area_id = None
        for p in pods:
            a = (p.get("area") or {}).get("id") if isinstance(p.get("area"), dict) else None
            if a:
                area_id = a
                break
        return {
            "id": eid,
            "name": (ev.get("name") or "").strip(),
            "area_id": area_id,
            "count": len(canceled),
            "start": parse_iso(ev.get("startTime")),
            "end": parse_iso(ev.get("endTime")),
        }

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for fut in as_completed([ex.submit(work, e) for e in ranked]):
            res = fut.result()
            if res:
                out[res["id"]] = res
    return out


# ----------------------------- Heatmap CSV + Drive upload -----------------------------

def fmt_util_cell(v: float | None) -> str:
    if v is None:
        return ""
    pct = round(v * 100)
    if v >= 0.65:
        return f"\U0001F7E2 {pct}%"
    if v >= 0.45:
        return f"\U0001F7E1 {pct}%"
    return f"\U0001F534 {pct}%"


def build_csv(util: dict, info: dict, today: date) -> str:
    dates = [today + timedelta(days=i) for i in range(14)]
    buf = io.StringIO()
    w = csv.writer(buf)

    header = ["Site"] + [d.strftime("%b %-d") for d in dates]
    w.writerow(header)

    # Map area name substring -> area_id for lookup
    name_to_aid = {v: k for k, v in info["area_name"].items()}

    for label, (name_key, sub_key) in HEATMAP_ROWS:
        # Find area_id by substring match
        area_id = None
        for n, aid in name_to_aid.items():
            if name_key in n:
                area_id = aid
                break
        row = [label]
        for d in dates:
            d_iso = d.isoformat()
            cell_val = None
            if area_id and area_id in util and d_iso in util[area_id]:
                per_day = util[area_id][d_iso]
                if isinstance(per_day, dict):
                    cell_val = per_day.get(sub_key)
                elif sub_key == "main":
                    cell_val = per_day
            row.append(fmt_util_cell(cell_val))
        w.writerow(row)

    return buf.getvalue()


# Candidate tool names to try when creating a file on the Drive MCP.
# claude.ai's Drive connector uses `create_file`; Google's drivemcp.googleapis.com
# may use different naming. We try each in order and use the first that works.
DRIVE_CREATE_CANDIDATES = [
    "create_file",
    "createFile",
    "drive_create_file",
    "files_create",
    "upload_file",
    "uploadFile",
]


def _url_from_meta(meta: dict) -> str | None:
    if not isinstance(meta, dict):
        return None
    url = meta.get("viewUrl") or meta.get("webViewLink") or meta.get("url")
    if url:
        return url
    fid = meta.get("id")
    if fid:
        return f"https://docs.google.com/spreadsheets/d/{fid}"
    return None


def _extract_file_url(resp: dict) -> str | None:
    """Try multiple response shapes to extract a Drive file URL or ID."""
    try:
        result = resp.get("result") or {}
        # Shape 1 (MCP 2025 spec): structuredContent holds the tool's return object
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            url = _url_from_meta(sc)
            if url:
                return url
        # Shape 2: result.content[].text contains JSON
        for c in (result.get("content") or []):
            text = c.get("text")
            if not text:
                continue
            try:
                meta = json.loads(text)
            except Exception:
                continue
            url = _url_from_meta(meta)
            if url:
                return url
        # Shape 3: direct metadata on result
        url = _url_from_meta(result)
        if url:
            return url
    except Exception:
        pass
    return None


def upload_sheet(
    csv_content: str,
    drive_url: str,
    token: str,
    today: date,
    errors: list[str],
    drive_tools: list[str] | None = None,
) -> str | None:
    """Upload CSV to Drive as a Google Sheet; return viewUrl or None on failure.
    Appends detailed diagnostics to `errors` so they show up in briefing_status.json."""
    b64 = base64.b64encode(csv_content.encode("utf-8")).decode("ascii")
    args = {
        "title": f"Fairgrounds Utilization {today.isoformat()}",
        "mimeType": "text/csv",
        "parentId": HEATMAP_FOLDER_ID,
        "content": b64,
    }

    # Prefer tools we know exist on this MCP; fall back to the full candidate list.
    if drive_tools:
        try_tools = [t for t in DRIVE_CREATE_CANDIDATES if t in drive_tools] or drive_tools
    else:
        try_tools = DRIVE_CREATE_CANDIDATES

    for tool in try_tools:
        try:
            resp = mcp_call(drive_url, token, tool, args)
        except urllib.error.HTTPError as he:
            body = ""
            try:
                body = he.read().decode()[:500]
            except Exception:
                pass
            errors.append(f"drive {tool}: HTTP {he.code} {body}")
            print(f"[upload_sheet] {tool} HTTP {he.code}: {body}", file=sys.stderr)
            continue
        except Exception as e:
            errors.append(f"drive {tool}: {type(e).__name__}: {e}")
            print(f"[upload_sheet] {tool} raised: {e}", file=sys.stderr)
            continue

        # Check for MCP-level error in response
        if isinstance(resp, dict) and "error" in resp:
            errors.append(f"drive {tool}: MCP error {resp['error']}")
            print(f"[upload_sheet] {tool} returned MCP error: {resp.get('error')}", file=sys.stderr)
            continue

        url = _extract_file_url(resp)
        if url:
            print(f"[upload_sheet] success via {tool}: {url}", file=sys.stderr)
            return url

        # Response looked ok but we couldn't extract a URL — log raw for debugging
        snippet = json.dumps(resp)[:500]
        errors.append(f"drive {tool}: could not parse response — {snippet}")
        print(f"[upload_sheet] {tool} response had no URL: {snippet}", file=sys.stderr)

    return None


# ----------------------------- Slack message rendering -----------------------------

def fmt_time_range(start: datetime | None, end: datetime | None, tz: ZoneInfo) -> str:
    if not start:
        return ""
    s = start.astimezone(tz)
    parts = [s.strftime("%-I:%M %p").lstrip("0")]
    if end:
        e = end.astimezone(tz)
        parts.append(e.strftime("%-I:%M %p").lstrip("0"))
    return " – ".join(parts)


def render_message(
    target: dict,
    util_today: float | dict | None,
    buckets: dict,
    private_count: int,
    programmed_count: int,
    cancellations: list,
    today: date,
    heatmap_url: str,
    tz: ZoneInfo,
) -> str:
    weekday = today.strftime("%A")
    date_label = today.strftime("%B %-d, %Y")

    lines = []
    lines.append(f"*{target['label']}*")
    lines.append(f"*Morning Briefing — {weekday}, {date_label}*")
    lines.append("")

    # Utilization headline
    if isinstance(util_today, dict):
        tot = util_today.get("total")
        ip  = util_today.get("indoor_padel")
        op  = util_today.get("outdoor_padel")
        pk  = util_today.get("pickle")
        def pct(v): return f"{round(v*100)}%" if v is not None else "—"
        lines.append(f"> *Utilization today: Total {pct(tot)} · Indoor Padel {pct(ip)} · Outdoor Padel {pct(op)} · Pickle {pct(pk)}*")
    else:
        val = f"{round(util_today*100)}%" if util_today is not None else "—"
        lines.append(f"> *Utilization today: {val}*")
    lines.append(f"> {programmed_count} programmed events · {private_count} private court bookings")
    lines.append("")
    lines.append(HANGUL_FILLER)
    lines.append(f"\U0001F4CA <{heatmap_url}|14-day utilization outlook>")

    def section(title: str, items: list, formatter):
        if not items:
            return
        lines.append("")
        lines.append(HANGUL_FILLER)
        lines.append(f"*{title}*")
        lines.append("")
        for it in items:
            lines.append(formatter(it))

    def event_fmt(ev):
        tr = fmt_time_range(ev.get("start"), ev.get("end"), tz)
        tail = f" — {ev['signups']} signed up" if ev.get("signups") else ""
        return f"• {ev['name']} — {tr}{tail}"

    def private_fmt(ev):
        tr = fmt_time_range(ev.get("start"), ev.get("end"), tz)
        courts = ev.get("court_count", 1)
        court_tag = f" ({courts} courts)" if courts > 1 else ""
        return f"• {ev['name']} — {tr}{court_tag}"

    section("Open Play",           buckets.get("Open Play", []), event_fmt)
    section("Leagues",              buckets.get("Leagues", []), event_fmt)
    section("Clinics & Lessons",    buckets.get("Clinics & Lessons", []), event_fmt)
    section("Private Events",       buckets.get("Private Events", []), private_fmt)

    if cancellations:
        lines.append("")
        lines.append(HANGUL_FILLER)
        lines.append("*Cancellations*")
        lines.append("")
        for c in cancellations:
            tr = fmt_time_range(c.get("start"), c.get("end"), tz)
            suffix = "cancellation" if c["count"] == 1 else "cancellations"
            lines.append(f"• {c['name']} — {c['count']} {suffix} — {tr}")

    return "\n".join(lines)


# ----------------------------- Orchestrator -----------------------------

def main() -> int:
    errors: list[str] = []
    status: dict = {"ok": False, "errors": errors, "sheet_url": None}

    try:
        mode = os.environ.get("MODE", "test").lower()
        channel_map = CHANNEL_MAP_TEST if mode == "test" else CHANNEL_MAP_PROD
        dry_run = bool(os.environ.get("DRY_RUN"))

        # Today in UTC (the briefing uses local day boundaries per pod tz, but API window starts today UTC)
        today = datetime.now(timezone.utc).date()
        tomorrow = today + timedelta(days=1)
        heatmap_end = today + timedelta(days=14)

        print(f"[prep] mode={mode} today={today} dry_run={dry_run}", file=sys.stderr)

        # --- Fetch areas ---
        info = fetch_areas()
        print(f"[prep] {len(info['areas'])} active areas with court pods", file=sys.stderr)

        # --- Fetch sessions + events in parallel ---
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_today_sessions = ex.submit(fetch_sessions, today, tomorrow)
            f_today_events   = ex.submit(fetch_events, today, tomorrow)
            f_forward        = ex.submit(fetch_sessions, today, heatmap_end)
            today_sessions = f_today_sessions.result()
            today_events   = f_today_events.result()
            forward_sessions = f_forward.result()

        print(f"[prep] today_sessions={len(today_sessions)} today_events={len(today_events)} forward_sessions={len(forward_sessions)}", file=sys.stderr)

        # --- Categorize today's events ---
        cat = categorize_events_by_area(today_events, info)

        # --- Cancellations ---
        try:
            cancellations = fetch_cancellations(today_events)
        except Exception as e:
            errors.append(f"cancellations: {e}")
            cancellations = {}
        print(f"[prep] cancellations={len(cancellations)}", file=sys.stderr)

        # --- Utilization (today + 14-day) ---
        today_util = compute_utilization(today_sessions, info)
        forward_util = compute_utilization(forward_sessions, info)

        # --- Build heatmap CSV + upload ---
        csv_content = build_csv(forward_util, info, today)
        # Write a local copy for debugging
        with open("/tmp/heatmap.csv", "w", encoding="utf-8") as f:
            f.write(csv_content)

        heatmap_url = None
        if not dry_run:
            slack_url, drive_url, token = mcp_discover()
            # Probe the Drive MCP's actual tool surface — helps when the trigger's
            # Drive MCP (e.g. drivemcp.googleapis.com) doesn't match claude.ai's schema.
            drive_tools: list[str] = []
            try:
                drive_tools = mcp_list_tools(drive_url, token)
                print(f"[prep] drive_tools={drive_tools}", file=sys.stderr)
                status["drive_tools"] = drive_tools
            except Exception as e:
                errors.append(f"drive tools/list failed: {type(e).__name__}: {e}")
                print(f"[prep] drive tools/list failed: {e}", file=sys.stderr)
            heatmap_url = upload_sheet(csv_content, drive_url, token, today, errors, drive_tools)
            if not heatmap_url:
                errors.append("Drive upload failed; using folder URL as fallback")
                heatmap_url = HEATMAP_FOLDER_URL
        else:
            heatmap_url = HEATMAP_FOLDER_URL + "  (dry run, not uploaded)"

        status["sheet_url"] = heatmap_url
        print(f"[prep] heatmap_url={heatmap_url}", file=sys.stderr)

        # --- Build Slack messages per target channel ---
        messages: dict[str, dict] = {}
        briefing_out = {
            "today": today.isoformat(),
            "mode": mode,
            "heatmap_url": heatmap_url,
            "heatmap_folder_url": HEATMAP_FOLDER_URL,
            "target_channels": [],
        }

        # Match area displayName substrings → target config
        for name_sub, target in channel_map.items():
            match_area = None
            for a in info["areas"]:
                if name_sub in a["displayName"].lower():
                    match_area = a
                    break
            if not match_area:
                errors.append(f"No area matched for '{name_sub}' (channel {target['channel_name']})")
                continue

            aid = match_area["id"]
            tz = ZoneInfo(match_area["timezone"])
            util_today_val = today_util.get(aid, {}).get(today.isoformat())
            buckets = cat["area_buckets"].get(aid, {c: [] for c in CATEGORIES_ORDER})
            private_count = cat["area_private_count"].get(aid, 0)
            programmed = cat["area_programmed_count"].get(aid, 0)
            area_cancels = [c for c in cancellations.values() if c.get("area_id") == aid]

            body = render_message(
                target=target,
                util_today=util_today_val,
                buckets=buckets,
                private_count=private_count,
                programmed_count=programmed,
                cancellations=area_cancels,
                today=today,
                heatmap_url=heatmap_url,
                tz=tz,
            )

            messages[target["channel_id"]] = {
                "name": target["channel_name"],
                "label": target["label"],
                "body": body,
            }
            briefing_out["target_channels"].append({
                "channel_id": target["channel_id"],
                "channel_name": target["channel_name"],
                "label": target["label"],
                "area_id": aid,
                "area_display_name": match_area["displayName"],
                "timezone": match_area["timezone"],
                "utilization_today": util_today_val,
                "programmed_events_count": programmed,
                "private_bookings_count": private_count,
                "cancellations_count": len(area_cancels),
            })

        # --- Write output files ---
        with open("/tmp/messages.json", "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        with open("/tmp/briefing_output.json", "w", encoding="utf-8") as f:
            json.dump(briefing_out, f, ensure_ascii=False, indent=2)

        status["ok"] = True
        status["message_count"] = len(messages)
    except Exception as e:
        errors.append(f"fatal: {e}\n{traceback.format_exc()}")
        status["ok"] = False

    with open("/tmp/briefing_status.json", "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2, default=str)

    print(json.dumps(status, ensure_ascii=False, default=str), file=sys.stdout)
    return 0 if status["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
