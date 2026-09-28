#!/usr/bin/env python3
"""
AWS Events API - Builder ID サインイン (Authorization Code + PKCE)

使い方:
    python3 aws_events_signin.py [eventId] [--no-abstracts] [--locale=ja-JP]

実行すると:
  1. PKCEパラメータを生成
  2. ローカルの http://localhost:8484/callback でリダイレクトを待ち受け
  3. 認可URLを表示（自動でブラウザも開こうとします）
  4. Builder IDでサインインすると、このスクリプトがcodeを受け取り
  5. 自動でアクセストークンに交換
  6. ListSessions（GET /v1/events/{eventId}/sessions）を nextToken が
     無くなるまでページネーションしながら全件取得し、JSONファイルに保存

オプション:
    eventId         : 対象イベントID（省略時 reinvent2026）
    --no-abstracts  : includeAbstracts=false を指定（abstractフィールドを省略、軽量化）
    --locale=xx-XX  : localeクエリパラメータを指定
    --reserve=id1,id2,...
                    : セッション一覧取得の代わりに、指定したセッションID（1〜10件、重複不可）を
                      予約する（ReserveSessions: POST /v1/events/{eventId}/reservations）
    --md            : セッション一覧取得時、JSONに加えて sessions-<eventId>-md/ 配下に
                      セッションIDごとの Markdown ファイル（<sessionId>.md）も保存する
"""

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

PORT = 8484
CLIENT_ID = "7vmom55m1qstvq8i71ph127bfq"
REDIRECT_URI = f"http://localhost:{PORT}/callback"
AUTH_ENDPOINT = "https://oauth.awsevents.com/oauth2/authorize"
TOKEN_ENDPOINT = "https://oauth.awsevents.com/oauth2/token"
SCOPE = "openid email events/access"
API_BASE = "https://api.awsevents.com/v1"

# ---- CLI引数のパース ----
_cli_args = sys.argv[1:]
EVENT_ID = next((a for a in _cli_args if not a.startswith("--")), "reinvent2026")
INCLUDE_ABSTRACTS = "--no-abstracts" not in _cli_args
_locale_arg = next((a for a in _cli_args if a.startswith("--locale=")), None)
LOCALE = _locale_arg.split("=", 1)[1] if _locale_arg else None
_reserve_arg = next((a for a in _cli_args if a.startswith("--reserve=")), None)
RESERVE_SESSION_IDS = (
    [s for s in _reserve_arg.split("=", 1)[1].split(",") if s] if _reserve_arg else None
)
MD_OUTPUT = "--md" in _cli_args

if RESERVE_SESSION_IDS is not None:
    if not (1 <= len(RESERVE_SESSION_IDS) <= 10):
        print("エラー: --reserve には1〜10件のセッションIDを指定してください（例: --reserve=id1,id2）", file=sys.stderr)
        sys.exit(1)
    if len(set(RESERVE_SESSION_IDS)) != len(RESERVE_SESSION_IDS):
        print("エラー: --reserve のセッションIDは重複できません。", file=sys.stderr)
        sys.exit(1)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---- PKCE生成 ----
code_verifier = b64url(secrets.token_bytes(48))  # 43-128文字のunreserved文字列
code_challenge = b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
state = secrets.token_hex(16)

# ---- 認可URLを組み立て ----
auth_params = {
    "response_type": "code",
    "client_id": CLIENT_ID,
    "redirect_uri": REDIRECT_URI,
    "scope": SCOPE,
    "identity_provider": "AWSBuilderID",
    "code_challenge": code_challenge,
    "code_challenge_method": "S256",
    "state": state,
}
auth_url = f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(auth_params)}"


# ---- トークンエンドポイントへPOST ----
def exchange_code_for_token(code: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        }
    ).encode("ascii")

    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Token endpoint returned {e.code}: {e.read().decode('utf-8')}")


# ---- 1ページ分のGETリクエスト ----
class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"API returned {status}: {body}")
        self.status = status
        self.body = body


def get_json(url: str, access_token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data)
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8"))


def post_json(url: str, access_token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode("utf-8"))


# ---- ReserveSessions: 指定したセッションIDを予約 ----
def reserve_sessions(event_id: str, access_token: str, session_ids: list) -> dict:
    url = f"{API_BASE}/events/{urllib.parse.quote(event_id, safe='')}/reservations"
    return post_json(url, access_token, {"sessionIds": session_ids})


# ---- ListSessions: nextTokenが無くなるまで全ページ取得 ----
# 注意: ページが短くても最後とは限らない。nextTokenが無いことだけが終了条件。
def fetch_all_sessions(event_id: str, access_token: str, include_abstracts: bool = True, locale: str = None):
    sessions = []
    next_token = None
    total_count = None
    page = 0

    while True:
        params = {"includeAbstracts": str(include_abstracts).lower()}
        if locale:
            params["locale"] = locale
        if next_token:
            params["nextToken"] = next_token

        url = f"{API_BASE}/events/{urllib.parse.quote(event_id, safe='')}/sessions?{urllib.parse.urlencode(params)}"

        body = get_json(url, access_token)
        page += 1
        if body.get("totalCount") is not None:
            total_count = body.get("totalCount")
        page_sessions = body.get("items") or []
        sessions.extend(page_sessions)
        next_token = body.get("nextToken")

        count_str = f" / {total_count}" if total_count is not None else ""
        print(f"  ページ{page}: {len(page_sessions)}件取得（累計 {len(sessions)}{count_str}）")

        if not next_token:
            break

    return sessions, total_count


# ---- セッション一覧をMarkdownに整形 ----
# 注意: 各フィールドのキー名はドキュメント上に明記が見当たらないため推測。
# イベントが実際に提供しないフィールドは省略して表示する。
def _first(session: dict, *keys):
    for k in keys:
        v = session.get(k)
        if v:
            return v
    return None


def _names(items) -> list:
    names = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get("name")
            if name:
                names.append(name)
        elif isinstance(item, str):
            names.append(item)
    return names


def session_to_markdown(session: dict) -> str:
    session_id = session.get("sessionId", "?")
    title = session.get("title") or "(タイトルなし)"
    lines = [f"# [{session_id}] {title}", ""]

    meta = []
    if session.get("abbreviation"):
        meta.append(f"略称: {session['abbreviation']}")
    code = _first(session, "code", "sessionCode")
    if code:
        meta.append(f"コード: {code}")
    session_type = _first(session, "sessionType", "type")
    if session_type:
        meta.append(f"タイプ: {session_type}")
    if session.get("level"):
        meta.append(f"レベル: {session['level']}")
    tracks = _names(_first(session, "tracks"))
    if tracks:
        meta.append(f"トラック: {', '.join(tracks)}")
    topics = _names(_first(session, "topics"))
    if topics:
        meta.append(f"トピック: {', '.join(topics)}")
    start = _first(session, "startDateTime", "startTime")
    end = _first(session, "endDateTime", "endTime")
    if start or end:
        meta.append(f"時間: {start or '?'} 〜 {end or '?'}")
    place = ", ".join(filter(None, [session.get("venue"), session.get("room")]))
    if place:
        meta.append(f"場所: {place}")
    speakers = _names(_first(session, "speakers"))
    if speakers:
        meta.append(f"スピーカー: {', '.join(speakers)}")

    for m in meta:
        lines.append(f"- {m}")
    if meta:
        lines.append("")

    if session.get("abstract"):
        lines.append(session["abstract"])
        lines.append("")

    return "\n".join(lines)


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


def save_sessions_as_markdown(event_id: str, sessions: list) -> str:
    md_dir = f"sessions-{event_id}-md"
    os.makedirs(md_dir, exist_ok=True)
    for i, s in enumerate(sessions):
        # ファイル名は abbreviation を優先。無ければ sessionId、それも無ければ連番。
        file_id = s.get("abbreviation") or s.get("sessionId") or f"unknown-{i}"
        safe_id = _UNSAFE_FILENAME_CHARS.sub("_", str(file_id))
        with open(os.path.join(md_dir, f"{safe_id}.md"), "w", encoding="utf-8") as f:
            f.write(session_to_markdown(s))
    return md_dir


# ---- コールバック用ローカルサーバー ----
class CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # デフォルトのアクセスログを抑制

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return

        query = urllib.parse.parse_qs(parsed.query)
        error = query.get("error", [None])[0]
        returned_state = query.get("state", [None])[0]
        code = query.get("code", [None])[0]

        if error:
            self._respond(400, f"サインインに失敗しました: {error}")
            print("認可エラー:", error, query.get("error_description", [""])[0], file=sys.stderr)
            self.server.result = {"error": error}
            return

        if returned_state != state:
            self._respond(400, "state が一致しません。CSRF対策のため処理を中断しました。")
            print("state不一致: expected", state, "got", returned_state, file=sys.stderr)
            self.server.result = {"error": "state_mismatch"}
            return

        self._respond(200, "サインインできました。このタブは閉じて構いません。")
        self.server.result = {"code": code}

    def _respond(self, status: int, message: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode("utf-8"))


def main():
    server = http.server.HTTPServer(("localhost", PORT), CallbackHandler)
    server.result = None

    print(f"ローカルサーバーを起動しました: {REDIRECT_URI}")
    print("\n以下のURLをブラウザで開いてBuilder IDでサインインしてください:\n")
    print(auth_url)
    print()

    webbrowser.open(auth_url)  # 失敗しても無視される

    # コールバックが来るまで1リクエスト分だけ待つ
    while server.result is None:
        server.handle_request()

    if "error" in server.result:
        return

    code = server.result["code"]

    try:
        tokens = exchange_code_for_token(code)
    except RuntimeError as e:
        print("トークン交換に失敗しました:", e)
        return

    print("\n=== トークン取得成功 ===")
    print("ACCESS_TOKEN :", tokens.get("access_token"))
    if tokens.get("refresh_token"):
        print("REFRESH_TOKEN:", tokens.get("refresh_token"))
    print("有効期限     :", tokens.get("expires_in"), "秒")

    if RESERVE_SESSION_IDS is not None:
        print(f"\n=== {EVENT_ID} のセッションを予約中: {', '.join(RESERVE_SESSION_IDS)} ===")
        try:
            result = reserve_sessions(EVENT_ID, tokens.get("access_token"), RESERVE_SESSION_IDS)

            print("\n=== 予約結果 ===")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # 注意: succeeded/failed のキー名は実APIで要確認（ListSessionsのitemsと同様の前提）
            succeeded = result.get("succeeded") or []
            failed = result.get("failed") or []
            if succeeded:
                print(f"\n成功: {len(succeeded)}件")
            if failed:
                print(f"失敗: {len(failed)}件（理由は上記JSONを参照。未知の理由コードは「拒否」として扱ってください）")

            out_file = f"reservation-{EVENT_ID}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"保存先: {out_file}")

        except ApiError as e:
            if e.status == 401:
                print("予約に失敗（401）: トークンが無効です。", file=sys.stderr)
            elif e.status == 403:
                print(
                    f"予約に失敗（403）: このイベント（{EVENT_ID}）に登録されていない可能性があります。"
                    "イベントの登録サイトで登録してから再実行してください。",
                    file=sys.stderr,
                )
            elif e.status == 400:
                print(
                    f"予約に失敗（400）: リクエスト内容を確認してください（セッションIDは1〜10件・重複不可）: {e.body}",
                    file=sys.stderr,
                )
            else:
                print("予約に失敗しました:", e, file=sys.stderr)
        return

    print(f"\n=== {EVENT_ID} のセッション一覧を取得中 ===")
    try:
        sessions, total_count = fetch_all_sessions(
            EVENT_ID, tokens.get("access_token"), include_abstracts=INCLUDE_ABSTRACTS, locale=LOCALE
        )

        count_note = f"（catalog上のtotalCount: {total_count}）" if total_count is not None else ""
        print(f"\n取得完了: {len(sessions)}件{count_note}")

        out_file = f"sessions-{EVENT_ID}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        print(f"保存先: {out_file}")

        if MD_OUTPUT:
            md_dir = save_sessions_as_markdown(EVENT_ID, sessions)
            print(f"保存先(Markdown): {md_dir}/ 配下に{len(sessions)}件")

        print("\n--- 先頭5件のタイトル ---")
        for s in sessions[:5]:
            print(f"- [{s.get('sessionId', '?')}] {s.get('title', '(タイトルなし)')}")

    except ApiError as e:
        if e.status == 401:
            print("セッション取得に失敗（401）: トークンが無効です。", file=sys.stderr)
        elif e.status == 403:
            print(
                f"セッション取得に失敗（403）: このイベント（{EVENT_ID}）に登録されていない可能性があります。"
                "イベントの登録サイトで登録してから再実行してください。",
                file=sys.stderr,
            )
        else:
            print("セッション取得に失敗しました:", e, file=sys.stderr)


if __name__ == "__main__":
    main()