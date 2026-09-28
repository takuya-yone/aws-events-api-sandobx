# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A small sandbox holding two standalone, dependency-free scripts (`aws_events_signin.py` and `aws_events_signin.js`) that do the same thing in Python and Node.js: sign in to the AWS Events API via AWS Builder ID (OAuth 2.0 Authorization Code + PKCE), then either call `ListSessions` or `ReserveSessions` and dump the result to a JSON file. There is no package.json, build step, test suite, or linter — the scripts run directly with `python3` / `node`.

Reference: https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html

## Running the scripts

```bash
# Python (3.x, stdlib only)
python3 aws_events_signin.py [eventId] [--no-abstracts] [--locale=ja-JP] [--md] [--reserve=id1,id2]

# Node.js (stdlib only, v18+ recommended)
node aws_events_signin.js [eventId] [--no-abstracts] [--locale=ja-JP] [--md] [--reserve=id1,id2]
```

- `eventId` (positional, default `reinvent2026`): target event.
- `--no-abstracts`: sends `includeAbstracts=false` to shrink the response.
- `--locale=xx-XX`: sets the `locale` query param.
- `--md`: listing path only — in addition to the JSON file, renders each session as its own human-readable Markdown file under `sessions-<eventId>-md/`, via `save_sessions_as_markdown` / `saveSessionsAsMarkdown` (which call `session_to_markdown` / `sessionToMarkdown` per session). Filename is `<abbreviation>.md`, falling back to `<sessionId>.md` then `unknown-<index>.md`.
- `--reserve=id1,id2,...`: instead of listing sessions, calls `ReserveSessions` (`POST /v1/events/{eventId}/reservations`) to reserve 1–10 distinct session IDs. Mutually exclusive with the listing flow (and thus with `--md`) — when set, listing is skipped entirely.

Both scripts require a real browser-based Builder ID sign-in during execution — there is no automated/headless test path. Output is written to `sessions-<eventId>.json` (+ optional `sessions-<eventId>-md/` directory) for listing, or `reservation-<eventId>.json` for `--reserve`, in the current directory.

## Architecture (same shape in both languages)

Each script is a single self-contained flow with no external modules:

1. **PKCE generation** — random `code_verifier`, SHA-256 `code_challenge`, and a `state` value for CSRF protection.
2. **Local callback server** on `http://localhost:8484/callback` — waits for exactly one redirect from the OAuth flow (Python: blocks on `handle_request()`; Node: `http.createServer`, closes itself after handling).
3. **Authorization URL** is printed and opened in the default browser automatically (best-effort; failure is ignored).
4. **Token exchange** — POSTs the authorization code + `code_verifier` to `TOKEN_ENDPOINT` to get an access token.
5. **Branch on `--reserve`**:
   - If set: **`reserve_sessions` / `reserveSessions`** POSTs `{"sessionIds": [...]}` to `/v1/events/{eventId}/reservations`, prints the raw response, and saves it to `reservation-<eventId>.json`. A `200` does not mean every ID succeeded — the API reports success/failure per session, and an already-reserved session counts as a failure, so this call is not safely retryable by re-sending the same IDs.
   - Otherwise: **Pagination loop (`fetch_all_sessions` / `fetchAllSessions`)** — calls `GET /v1/events/{eventId}/sessions` repeatedly, following `nextToken` until absent. Termination is driven *only* by the absence of `nextToken`, not by page size — a short page is not necessarily the last one.
6. **Save + summary** (listing path only) — writes the full session array to `sessions-<eventId>.json`, optionally renders one Markdown file per session under `sessions-<eventId>-md/` when `--md` is set (filenames prefer `abbreviation`, fall back to `sessionId`, then `unknown-<index>`, with unsafe characters replaced, so entries never collide), and prints the first 5 titles. The reserve path saves under its own filename, see above.

Key constants at the top of each file (`CLIENT_ID`, `PORT`, `AUTH_ENDPOINT`, `TOKEN_ENDPOINT`, `API_BASE`, `SCOPE`) are duplicated across both scripts — when changing one (e.g. rotating the port or client ID), update both files to keep them in sync.

### Error handling conventions

- `401` from the sessions API → access token invalid, must re-run sign-in.
- `403` → the signed-in Builder ID isn't registered for the target event; register on the event's site first.
- `state` mismatch on the callback → possible CSRF or a stale/cross-session callback; abort and restart the flow.
- Port conflict on `8484`: change the `PORT` constant in *both* scripts to a value in the allowed range `8485`–`8489`, and make sure the new redirect URI matches what's registered with Builder ID.

### Known uncertainty

- The actual key name for the sessions array in the `ListSessions` response (`items` vs `sessions`, etc.) isn't confirmed in AWS docs. Both scripts currently read `body.items` / `body.get("items")`. If a real API call shows a different key, fix it in both `fetch_all_sessions` (Python) and `fetchAllSessions` (Node) — same fix, both files.
- Likewise, the `ReserveSessions` response's success/failure field names (`succeeded`/`failed` in this code) are a guess — AWS's docs describe the per-session success/failure behavior in prose but don't give a JSON schema. Both scripts print and save the raw response regardless, so this only affects the succeeded/failed *count* summary line; fix it in both files once the real keys are confirmed against a live response.
- Same for the per-session field names the `--md` renderer reads (`abbreviation`/`code`/`sessionType`/`tracks`/`topics`/`startDateTime`/`endDateTime`/`room`/`venue`/`speakers`) — AWS's docs describe what a session *contains* in prose (title, abstract, code, type/level, taxonomy lists including tracks and topics, start/end/room/venue, speaker names) without giving exact JSON keys, and don't mention an `abbreviation` field at all. The renderer is defensive (`_first`/`first` try multiple candidate keys, missing fields are silently omitted), so wrong guesses degrade gracefully rather than crashing — but fix the candidate key lists in both `session_to_markdown` (Python) and `sessionToMarkdown` (Node), and the filename fallback in `save_sessions_as_markdown` / `saveSessionsAsMarkdown`, once real field names are confirmed.
