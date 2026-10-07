#!/usr/bin/env python3
"""reserve.txt のセッションコードから sessions-<eventId>.json を引いてスケジュールMarkdownを生成する。"""
import json, sys
from datetime import datetime, timedelta

event = sys.argv[1] if len(sys.argv) > 1 else "reinvent2026"
by_code = {s.get("abbreviation"): s for s in json.load(open(f"sessions-{event}.json"))}
codes = [l.strip() for l in open("reserve.txt") if l.strip()]

rows, missing = [], []
for c in codes:
    s = by_code.get(c)
    if not s:
        missing.append(c)
        continue
    t = s.get("sessionTime", {})
    start = datetime.fromisoformat(f"{t['date']} {t['time']}")
    end = start + timedelta(minutes=int(t.get("length", 0)))
    rows.append((start, end, s))
rows.sort(key=lambda r: r[0])

out = [f"# {event} スケジュール", ""]
day = None
for start, end, s in rows:
    if start.date() != day:
        day = start.date()
        out += [f"## {day:%Y-%m-%d} ({'月火水木金土日'[day.weekday()]})", ""]
    out += [
        f"### {start:%H:%M}–{end:%H:%M} [{s['abbreviation']}] {s['title']}",
        f"- 種別: {s.get('type', '')} / レベル: {s.get('level', '')}",
        "- 会場: " + " / ".join(x for x in (s.get("venue"), s.get("room")) if x),
    ]
    if s.get("topics"): out.append(f"- トピック: {', '.join(s['topics'])}")
    if s.get("services"): out.append(f"- サービス: {', '.join(s['services'])}")
    out += ["", s.get("abstract", ""), ""]
if missing:
    out += ["## 未発見のコード", ""] + [f"- {c}" for c in missing] + [""]
open(f"schedule-{event}.md", "w").write("\n".join(out))
print(f"{len(rows)} sessions, missing: {missing}")
