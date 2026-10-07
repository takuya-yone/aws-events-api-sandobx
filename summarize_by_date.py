#!/usr/bin/env python3
"""
favorites-<eventId>.json（または sessions-<eventId>.json）を日付ごとに集計する。
サインイン不要、標準ライブラリのみ。

使い方:
    python3 summarize_by_date.py [json_file] [--list] [--md[=out_dir]]

    json_file : 対象JSON（省略時 favorites-reinvent2026.json）
    --list    : 日付ごとの件数に加えて、各日のセッション一覧（時刻順）も表示する
    --md      : 日付ごとに別ファイルで、会場 → 開始時刻 の階層のMarkdownを出力する
                （省略時の出力先ディレクトリは <json_fileの拡張子を除いた名前>-schedule/、
                  ファイル名は <日付>.md。日時なしは unknown-date.md）
"""

import collections
import json
import os
import sys

args = sys.argv[1:]
path = next((a for a in args if not a.startswith("--")), "favorites-reinvent2026.json")
show_list = "--list" in args
_md_arg = next((a for a in args if a == "--md" or a.startswith("--md=")), None)
md_dir = None
if _md_arg:
    md_dir = _md_arg.split("=", 1)[1] if "=" in _md_arg else path.rsplit(".", 1)[0] + "-schedule"

with open(path, encoding="utf-8") as f:
    sessions = json.load(f)

by_date = collections.defaultdict(list)
for s in sessions:
    st = s.get("sessionTime") or {}
    by_date[st.get("date") or "(日時なし)"].append(s)


def sort_key(s):
    return (s.get("sessionTime") or {}).get("time") or ""


print(f"{path}: 合計 {len(sessions)}件\n")
for date in sorted(by_date):
    items = sorted(by_date[date], key=sort_key)
    types = collections.Counter(s.get("type") or "?" for s in items)
    type_str = ", ".join(f"{t} {n}" for t, n in types.most_common())
    print(f"{date}  {len(items):3d}件  ({type_str})")
    if show_list:
        for s in items:
            st = s.get("sessionTime") or {}
            length = f"{st['length']}分" if st.get("length") else "-"
            print(f"    {st.get('time', '--:--')} {length:>6}  [{s.get('abbreviation') or s.get('sessionId')}] {s.get('title')}")
        print()


# ---- 日付ごとに別ファイル（会場 → 開始時刻）のMarkdown ----
def venue_of(s):
    # venue が空のセッションが多いため、room の先頭（"Wynn/Encore | Level 1 | ..."）で補う
    if s.get("venue"):
        return s["venue"]
    room = s.get("room") or ""
    return room.split("|")[0].strip() or "(会場不明)"


def room_of(s):
    # roomが "<会場> | ..." で始まる場合は会場名の部分を除いて表示する
    room = s.get("room") or ""
    parts = [p.strip() for p in room.split("|")]
    if len(parts) > 1 and parts[0] == venue_of(s):
        parts = parts[1:]
    return " | ".join(p for p in parts if p) or "-"


def end_time(time, length):
    try:
        h, m = map(int, time.split(":"))
        total = h * 60 + m + int(length)
        return f"{total // 60 % 24:02d}:{total % 60:02d}"
    except (ValueError, AttributeError):
        return None


if md_dir:
    os.makedirs(md_dir, exist_ok=True)
    for date in sorted(by_date):
        lines = [f"# {date}（{len(by_date[date])}件）", ""]
        by_venue = collections.defaultdict(list)
        for s in by_date[date]:
            by_venue[venue_of(s)].append(s)
        for venue in sorted(by_venue):
            lines += [f"## {venue}（{len(by_venue[venue])}件）", ""]
            by_time = collections.defaultdict(list)
            for s in by_venue[venue]:
                by_time[(s.get("sessionTime") or {}).get("time") or "(時刻なし)"].append(s)
            for time in sorted(by_time):
                lines += [f"### {time}", ""]
                for s in sorted(by_time[time], key=lambda x: x.get("abbreviation") or ""):
                    st = s.get("sessionTime") or {}
                    end = end_time(st.get("time"), st.get("length"))
                    span = f"{st['time']}-{end}" if end else (f"{st['length']}分" if st.get("length") else "")
                    code = s.get("abbreviation") or s.get("sessionId")
                    lines.append(f"- **[{code}] {s.get('title')}**")
                    detail = [d for d in (s.get("type"), span, room_of(s)) if d and d != "-"]
                    if detail:
                        lines.append(f"  - {' / '.join(detail)}")
                lines.append("")
        file_name = "unknown-date" if date.startswith("(") else date
        with open(os.path.join(md_dir, f"{file_name}.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    print(f"\n保存先(Markdown): {md_dir}/ 配下に{len(by_date)}ファイル")
