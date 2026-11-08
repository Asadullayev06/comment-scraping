---
name: ops-intelligence
description: Operatives Social Media Intelligence Bridge (ops) & Google Sheets CLI (sheets) for Hermes Agent.
---

# Operatives Intelligence (`ops`) & Google Sheets (`sheets`) Skill

> **CRITICAL EXECUTION & ANTI-LOOPING RULES (READ FIRST — NON-NEGOTIABLE):**
>
> 1. **BANNED Commands & Loops:**
>    - **NEVER** wrap `ops video` or `curl .../api/video` inside a shell `for` or `while` loop.
>    - **NEVER** run `sleep` > 10s to poll background tasks.
>    - **NEVER** write custom Python/bash transcript scrapers (`vidjob.py`, `yt-dlp` subtitle brute-forcing, Invidious, Piped, tactiq, etc.) — YouTube bot-blocks this IP, and `transcript.available: false` with `extraction_engine: "gemini"` is the normal Gemini video-grounded stub. Always read `analysis.summary`, `key_moments`, and `claims_and_allegations`.
> 2. **Single-Attempt & Immediate Fallback Rule:**
>    - Call `ops video` / `/api/video` at most **ONCE** per video.
>    - If `/api/video` returns `504`, `deterministic_failure: true`, or fails once, **STOP IMMEDIATELY**. Do not retry. Fall back immediately to the already-fetched Telegram posts (`tgclient`) and YouTube metadata/comments (`/api/scrape` + `/api/check`) to answer the user's question within 60 seconds, and note in one line that full video stream ingestion timed out.
> 3. **Company Finances & Spreadsheets Rule:**
>    - When asked about company finances or spreadsheets, use the `sheets` CLI (`/usr/local/bin/sheets`) to inspect and update Google Sheets.

## 1. Command Reference Table

| Tool / Command | Purpose | Notes |
| :--- | :--- | :--- |
| `ops video "<url>"` | Analyze a YouTube video via Google Gemini (Option 1: video summary, key moments, claims) | **Call at most ONCE per video.** Never loop or retry on `504` / `deterministic_failure: true`. |
| `ops video "<url>" --reactions` | Analyze YouTube video content + viewer comment reactions (Option 2) | Pass `--query "<question>"` to answer a specific question. |
| `ops scrape "<url>"` | Scrape & evaluate comments on a Telegram, YouTube, Instagram, X, or Facebook post | Fast comment & metadata pipeline. |
| `ops profile "<handle_or_url>"` | Analyze a profile across its last N posts (`--posts 3`) | Supports Instagram, X, Facebook, YouTube. |
| `ops posts "<handle_or_url>"` | List recent posts for a profile with counts & shortcodes | Supports `--count 5`. |
| `ops replies "<x_handle>"` | Fetch & score an X user's own recent replies | Uses `TweetType.Replies`. |
| `ops digest --hours 24` | Summarize flagged hostile comments across platforms | Supports `--platform` and `--min-score`. |
| `ops check "<target>"` | Inspect stored stats & flagged comments for a target | Use `ops check list` to list all targets. |
| `ops search "<keyword>"` | Search stored comments in PostgreSQL | Supports `--platform`, `--min-score`, `--failed`. |
| `ops sessions` | Inspect burner session health or add/reset sessions | Supports `--reset` and `--add`. |
| `sheets list` | List all Google Sheets in the configured Google Drive folder | Returns spreadsheet names and IDs. |
| `sheets read "<name_or_id>" [range]` | Read a Google Sheet or specific range (e.g. `A1:E20`) | Outputs clean JSON rows to stdout. |
| `sheets update "<name_or_id>" "<range>" "<values_json>"` | Write values to a cell or range | Example: `sheets update "Budget" "B5" '[[2500]]'` |
| `sheets append "<name_or_id>" "<values_json>"` | Append a row to a Google Sheet | Example: `sheets append "Finances" '["2026-11-08", "AWS", -120]'` |
