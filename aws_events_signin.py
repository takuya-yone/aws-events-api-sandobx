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
"""

import base64
import hashlib
import http.server
import json
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