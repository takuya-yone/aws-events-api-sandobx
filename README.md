# AWS Events API セッション取得スクリプト

AWS Events API の `ListSessions`(セッション一覧取得)を呼び出すため、Builder ID による
OAuth 2.0 認可コードフロー(PKCE)でサインインし、取得したアクセストークンで
セッション一覧を全件取得・JSON保存するスクリプトです。Node.js版とPython版があり、
どちらも**外部ライブラリ不要**(標準ライブラリのみ)で動作します。

Ref. https://docs.aws.amazon.com/events/latest/devguide/what-is-events-api.html

## プログラムのサマリ

対象イベント(デフォルト `reinvent2026`)は登録制のため、匿名アクセスでは
`{"message":"Sign in to continue"}` が返ります。本スクリプトは以下を自動化します。

1. PKCE用の `code_verifier` / `code_challenge` / `state` を生成
2. ローカルに `http://localhost:8484/callback` を待ち受けるサーバーを起動
3. 認可URLを表示し、可能であれば既定ブラウザで自動的に開く
4. ユーザーがBuilder IDでサインイン → ブラウザがローカルサーバーにリダイレクト
5. 受け取った認可コードをアクセストークンに交換(トークンエンドポイントへPOST)
6. アクセストークンを使い `GET /v1/events/{eventId}/sessions` を呼び出し、
   `nextToken` が無くなるまでページネーションして全件取得
7. 取得結果を `sessions-<eventId>.json` に保存し、先頭5件のタイトルを表示

`--md` を指定すると、JSON保存に加えて `sessions-<eventId>-md/` ディレクトリに
セッションごとのMarkdownファイルも保存します。ファイル名はレスポンス内の
`abbreviation` フィールド(無ければ `sessionId`、それも無ければ連番)を使います。
各ファイルには略称・タイトル・コード・トラック・トピック・サービス・セッション時間
(`sessionTime`。無ければ開始/終了時刻)・場所・スピーカー・abstractを整形して出力します。

`--favorites` を指定した場合は、6〜7の代わりに
`GET /v1/events/{eventId}/schedule`
([GetSchedule](https://docs.aws.amazon.com/events/latest/devguide/rest-op-getschedule.html))
で自分のお気に入りのセッションID(`schedule.favorites`)を取得し、ListSessions の
全件結果と突き合わせて詳細付きで `favorites-<eventId>.json` に保存します
(`--md` 併用時は `favorites-<eventId>-md/` にも出力)。GetSchedule はIDしか返さないため
この方式にしています。カタログに見つからないIDは警告表示されます。

`--reserve=id1,id2,...` を指定した場合は、6〜7の代わりに
`POST /v1/events/{eventId}/reservations`
([ReserveSessions](https://docs.aws.amazon.com/events/latest/devguide/rest-op-reservesessions.html))
を呼び出し、指定したセッション(1〜10件、重複不可)を予約します。結果は
`reservation-<eventId>.json` に保存されます。レスポンスは成功/失敗をセッションごとに
返し、`200` でも一部が失敗している場合があるため、常に失敗内容を確認してください
(すでに予約済みのセッションも失敗として扱われるため、同じリクエストの再送は安全な
リトライにはなりません)。

### 主なオプション(Node版・Python版共通)

| オプション | 説明 |
|---|---|
| `eventId`(第1引数) | 対象イベントID。省略時は `reinvent2026` |
| `--no-abstracts` | `includeAbstracts=false` を指定し、abstractフィールドを省略して軽量化 |
| `--locale=xx-XX` | `locale` クエリパラメータを指定(例: `--locale=ja-JP`) |
| `--md` | セッション一覧/お気に入り取得時、JSONに加えて `sessions-<eventId>-md/`(お気に入りは `favorites-<eventId>-md/`)にセッションごとのMarkdownも保存 |
| `--favorites` | セッション一覧取得の代わりに、お気に入り登録したセッションの詳細を取得(`--reserve` と併用不可) |
| `--reserve=id1,id2,...` | セッション一覧取得の代わりに、指定したセッションID(1〜10件、重複不可)を予約 |

### エラー時の挙動

| ステータス | 意味 | 対処 |
|---|---|---|
| `401` | トークンが無効 | サインインからやり直す |
| `403` | 対象イベントに未登録 | イベントの登録サイトで事前登録してから再実行 |
| `state不一致` | CSRF検知、または別セッションのコールバック | 最初からやり直す |

---

## Node.js での実行方法

### 必要環境

- Node.js(バージョン不問、標準モジュールのみ使用。動作確認は v18 以降推奨)

### 実行コマンド

```bash
# デフォルト(reinvent2026、abstract込み)
node aws-events-signin.js

# イベントIDを指定
node aws-events-signin.js some-summit-2026

# abstractを省略して軽量化
node aws-events-signin.js reinvent2026 --no-abstracts

# ロケールを指定
node aws-events-signin.js reinvent2026 --locale=ja-JP
```

### 実行の流れ

1. コンソールに認可URLが表示され、ブラウザが自動的に開く
2. ブラウザでBuilder IDにサインイン
3. サインイン完了後、コンソールにトークン取得成功のログが出力される
4. 続けてセッション取得が始まり、ページごとの進捗が表示される
5. 完了すると `sessions-<eventId>.json` が実行ディレクトリに保存される

---

## Python での実行方法

### 必要環境

- Python 3.x(標準ライブラリのみ使用。外部パッケージのインストール不要)

### 実行コマンド

```bash
# デフォルト(reinvent2026、abstract込み)
python3 aws_events_signin.py

# イベントIDを指定
python3 aws_events_signin.py some-summit-2026

# abstractを省略して軽量化
python3 aws_events_signin.py reinvent2026 --no-abstracts

# ロケールを指定
python3 aws_events_signin.py reinvent2026 --locale=ja-JP
```

### 実行の流れ

Node.js版と同一です(表示メッセージも日本語で揃えてあります)。

---

## 補足: ポート競合時の対処

どちらの版もローカルの `8484` ポートで待ち受けます。既に使用中の場合は、
スクリプト冒頭の `PORT`(Node版)/ `PORT`(Python版)の値を、
ドキュメントに記載の許可済みポート範囲(`8485`〜`8489`)内に変更してください。
変更した場合、Builder ID側に登録されている `redirect_uri` の許可設定と
一致している必要があります。

## 補足: レスポンスのフィールド名について

`ListSessions` のレスポンスにあるセッション配列のキー名(`items` か `sessions` か等)は、
ドキュメント上に明記が見当たらなかったため、実際にAPIを一度叩いて確認することを
推奨します。異なっていた場合は、両スクリプトの `fetchAllSessions` /
`fetch_all_sessions` 内の該当箇所を実際のキー名に合わせて修正してください。

同様に `ReserveSessions` のレスポンスにある成功/失敗一覧のキー名(`succeeded` /
`failed` 等)もドキュメント上に明記がないため仮の名前で実装しています。両スクリプトは
レスポンス全体をそのまま表示・保存するので実害はありませんが、成功/失敗件数の
サマリ表示部分(`reserveSessions` 呼び出し後の `result.succeeded` / `result.failed`
参照箇所)は実際のキー名に合わせて修正してください。

`--md` のMarkdown出力(`session_to_markdown` / `sessionToMarkdown`)が参照する
セッションの各フィールド名(`code`、`sessionType`、`tracks`、`topics`、`services`、
`sessionTime`、`startDateTime`、`endDateTime`、`room`、`venue`、`speakers` など)も、
AWSドキュメントには存在するフィールドの説明のみで正確なJSONキー名の記載がないため
推測です。イベントが実際に提供しないフィールドはMarkdown上で自動的に省略されるため
実害はありませんが、キー名が異なる場合は該当箇所を実際のレスポンスに合わせて
修正してください。`sessionTime` が存在する場合はそちらを優先して表示し、無い場合のみ
`startDateTime`/`endDateTime`(またはその別名)から時間を組み立てます。

なお、ファイル名に使う `abbreviation` フィールドもドキュメント上に明記が見当たらないため
推測です。存在しない場合は `sessionId` に、それも無い場合は `unknown-<インデックス>` に
フォールバックするため、複数件あっても上書きされません。