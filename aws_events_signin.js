#!/usr/bin/env node
'use strict';

/**
 * AWS Events API - Builder ID サインイン (Authorization Code + PKCE)
 *
 * 使い方:
 *   node aws-events-signin.js
 *
 * 実行すると:
 *   1. PKCEパラメータを生成
 *   2. ローカルの http://localhost:8484/callback でリダイレクトを待ち受け
 *   3. 認可URLを表示（自動でブラウザも開こうとします）
 *   4. Builder IDでサインインすると、このスクリプトがcodeを受け取り
 *   5. 自動でアクセストークンに交換
 *   6. ListSessions（GET /v1/events/{eventId}/sessions）を nextToken が
 *      無くなるまでページネーションしながら全件取得し、JSONファイルに保存
 *
 * トークン取得後は、そのままListSessions（イベントのセッション一覧）を
 * 全ページ取得してJSONファイルに保存します。
 *
 * 使い方（オプション）:
 *   node aws-events-signin.js [eventId] [--no-abstracts] [--locale=ja-JP]
 *
 *   eventId         : 対象イベントID（省略時 reinvent2026）
 *   --no-abstracts  : includeAbstracts=false を指定（abstractフィールドを省略、軽量化）
 *   --locale=xx-XX  : localeクエリパラメータを指定
 *   --reserve=id1,id2,...
 *                   : セッション一覧取得の代わりに、指定したセッションID（1〜10件、重複不可）を
 *                     予約する（ReserveSessions: POST /v1/events/{eventId}/reservations）
 *   --md            : セッション一覧取得時、JSONに加えて sessions-<eventId>-md/ 配下に
 *                     セッションIDごとの Markdown ファイル（<sessionId>.md）も保存する
 */

const http = require('http');
const https = require('https');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const { exec } = require('child_process');

const PORT = 8484;
const CLIENT_ID = '7vmom55m1qstvq8i71ph127bfq';
const REDIRECT_URI = `http://localhost:${PORT}/callback`;
const AUTH_ENDPOINT = 'https://oauth.awsevents.com/oauth2/authorize';
const TOKEN_ENDPOINT = 'https://oauth.awsevents.com/oauth2/token';
const SCOPE = 'openid email events/access';
const API_BASE = 'https://api.awsevents.com/v1';

// ---- CLI引数のパース ----
const cliArgs = process.argv.slice(2);
const EVENT_ID = cliArgs.find((a) => !a.startsWith('--')) || 'reinvent2026';
const INCLUDE_ABSTRACTS = !cliArgs.includes('--no-abstracts');
const LOCALE = (cliArgs.find((a) => a.startsWith('--locale=')) || '').split('=')[1];
const reserveArg = cliArgs.find((a) => a.startsWith('--reserve='));
const RESERVE_SESSION_IDS = reserveArg
  ? reserveArg.slice('--reserve='.length).split(',').filter(Boolean)
  : null;

if (RESERVE_SESSION_IDS) {
  if (RESERVE_SESSION_IDS.length < 1 || RESERVE_SESSION_IDS.length > 10) {
    console.error('エラー: --reserve には1〜10件のセッションIDを指定してください（例: --reserve=id1,id2）');
    process.exit(1);
  }
  if (new Set(RESERVE_SESSION_IDS).size !== RESERVE_SESSION_IDS.length) {
    console.error('エラー: --reserve のセッションIDは重複できません。');
    process.exit(1);
  }
}
const MD_OUTPUT = cliArgs.includes('--md');

// ---- PKCE生成 ----
function base64url(buf) {
  return buf.toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const codeVerifier = base64url(crypto.randomBytes(48)); // 43-128文字のunreserved文字列
const codeChallenge = base64url(crypto.createHash('sha256').update(codeVerifier).digest());
const state = crypto.randomBytes(16).toString('hex');

// ---- 認可URLを組み立て ----
const authUrl = new URL(AUTH_ENDPOINT);
authUrl.searchParams.set('response_type', 'code');
authUrl.searchParams.set('client_id', CLIENT_ID);
authUrl.searchParams.set('redirect_uri', REDIRECT_URI);
authUrl.searchParams.set('scope', SCOPE);
authUrl.searchParams.set('identity_provider', 'AWSBuilderID');
authUrl.searchParams.set('code_challenge', codeChallenge);
authUrl.searchParams.set('code_challenge_method', 'S256');
authUrl.searchParams.set('state', state);

// ---- トークンエンドポイントへPOST ----
function exchangeCodeForToken(code) {
  return new Promise((resolve, reject) => {
    const body = new URLSearchParams({
      grant_type: 'authorization_code',
      client_id: CLIENT_ID,
      redirect_uri: REDIRECT_URI,
      code,
      code_verifier: codeVerifier,
    }).toString();

    const req = https.request(
      TOKEN_ENDPOINT,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
          'Content-Length': Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            resolve(JSON.parse(data));
          } else {
            reject(new Error(`Token endpoint returned ${res.statusCode}: ${data}`));
          }
        });
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ---- 1ページ分のGETリクエスト ----
function getJson(urlObj, accessToken) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      urlObj,
      {
        method: 'GET',
        headers: { Authorization: `Bearer ${accessToken}` },
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve({ status: res.statusCode, body: JSON.parse(data) });
            } catch (e) {
              reject(new Error(`JSONパースに失敗: ${data}`));
            }
          } else {
            reject(
              Object.assign(new Error(`API returned ${res.statusCode}: ${data}`), {
                status: res.statusCode,
                body: data,
              })
            );
          }
        });
      }
    );
    req.on('error', reject);
    req.end();
  });
}

// ---- POST（JSONボディ）リクエスト ----
function postJson(urlObj, accessToken, payload) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(payload);
    const req = https.request(
      urlObj,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${accessToken}`,
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
        },
      },
      (res) => {
        let data = '';
        res.on('data', (chunk) => (data += chunk));
        res.on('end', () => {
          if (res.statusCode >= 200 && res.statusCode < 300) {
            try {
              resolve({ status: res.statusCode, body: JSON.parse(data) });
            } catch (e) {
              reject(new Error(`JSONパースに失敗: ${data}`));
            }
          } else {
            reject(
              Object.assign(new Error(`API returned ${res.statusCode}: ${data}`), {
                status: res.statusCode,
                body: data,
              })
            );
          }
        });
      }
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ---- ReserveSessions: 指定したセッションIDを予約 ----
async function reserveSessions(eventId, accessToken, sessionIds) {
  const urlObj = new URL(`${API_BASE}/events/${encodeURIComponent(eventId)}/reservations`);
  const { body } = await postJson(urlObj, accessToken, { sessionIds });
  return body;
}

// ---- ListSessions: nextTokenが無くなるまで全ページ取得 ----
// 注意: ページが短くても最後とは限らない。nextTokenが無いことだけが終了条件。
async function fetchAllSessions(eventId, accessToken, { includeAbstracts = true, locale } = {}) {
  const sessions = [];
  let nextToken;
  let totalCount;
  let page = 0;

  do {
    const urlObj = new URL(`${API_BASE}/events/${encodeURIComponent(eventId)}/sessions`);
    urlObj.searchParams.set('includeAbstracts', String(includeAbstracts));
    if (locale) urlObj.searchParams.set('locale', locale);
    if (nextToken) urlObj.searchParams.set('nextToken', nextToken);

    const { body } = await getJson(urlObj, accessToken);
    page += 1;
    totalCount = body.totalCount ?? totalCount;
    const pageSessions = body.items || [];
    sessions.push(...pageSessions);
    nextToken = body.nextToken;

    console.log(`  ページ${page}: ${pageSessions.length}件取得（累計 ${sessions.length}${totalCount != null ? ` / ${totalCount}` : ''}）`);
  } while (nextToken);

  return { sessions, totalCount };
}

// ---- セッション一覧をMarkdownに整形 ----
// 注意: 各フィールドのキー名はドキュメント上に明記が見当たらないため推測。
// イベントが実際に提供しないフィールドは省略して表示する。
function first(session, ...keys) {
  for (const k of keys) {
    if (session[k]) return session[k];
  }
  return undefined;
}

function names(items) {
  return (items || [])
    .map((item) => (typeof item === 'string' ? item : item?.name))
    .filter(Boolean);
}

function sessionToMarkdown(session) {
  const sessionId = session.sessionId ?? '?';
  const title = session.title || '(タイトルなし)';
  const lines = [`# [${sessionId}] ${title}`, ''];

  const meta = [];
  if (session.abbreviation) meta.push(`略称: ${session.abbreviation}`);
  const code = first(session, 'code', 'sessionCode');
  if (code) meta.push(`コード: ${code}`);
  const sessionType = first(session, 'sessionType', 'type');
  if (sessionType) meta.push(`タイプ: ${sessionType}`);
  if (session.level) meta.push(`レベル: ${session.level}`);
  const tracks = names(session.tracks);
  if (tracks.length) meta.push(`トラック: ${tracks.join(', ')}`);
  const topics = names(session.topics);
  if (topics.length) meta.push(`トピック: ${topics.join(', ')}`);
  const start = first(session, 'startDateTime', 'startTime');
  const end = first(session, 'endDateTime', 'endTime');
  if (start || end) meta.push(`時間: ${start ?? '?'} 〜 ${end ?? '?'}`);
  const place = [session.venue, session.room].filter(Boolean).join(', ');
  if (place) meta.push(`場所: ${place}`);
  const speakers = names(session.speakers);
  if (speakers.length) meta.push(`スピーカー: ${speakers.join(', ')}`);

  meta.forEach((m) => lines.push(`- ${m}`));
  if (meta.length) lines.push('');

  if (session.abstract) {
    lines.push(session.abstract);
    lines.push('');
  }

  return lines.join('\n');
}

const UNSAFE_FILENAME_CHARS = /[^A-Za-z0-9_.-]/g;

function saveSessionsAsMarkdown(eventId, sessions) {
  const mdDir = `sessions-${eventId}-md`;
  fs.mkdirSync(mdDir, { recursive: true });
  sessions.forEach((s, i) => {
    // ファイル名は abbreviation を優先。無ければ sessionId、それも無ければ連番。
    const fileId = s.abbreviation || s.sessionId || `unknown-${i}`;
    const safeId = String(fileId).replace(UNSAFE_FILENAME_CHARS, '_');
    fs.writeFileSync(path.join(mdDir, `${safeId}.md`), sessionToMarkdown(s), 'utf-8');
  });
  return mdDir;
}

// ---- コールバック用ローカルサーバー ----
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (url.pathname !== '/callback') {
    res.writeHead(404);
    res.end();
    return;
  }

  const returnedState = url.searchParams.get('state');
  const code = url.searchParams.get('code');
  const error = url.searchParams.get('error');

  if (error) {
    res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end(`サインインに失敗しました: ${error}`);
    console.error('認可エラー:', error, url.searchParams.get('error_description'));
    server.close();
    return;
  }

  if (returnedState !== state) {
    res.writeHead(400, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('state が一致しません。CSRF対策のため処理を中断しました。');
    console.error('state不一致: expected', state, 'got', returnedState);
    server.close();
    return;
  }

  res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end('サインインできました。このタブは閉じて構いません。');

  try {
    const tokens = await exchangeCodeForToken(code);
    console.log('\n=== トークン取得成功 ===');
    console.log('ACCESS_TOKEN :', tokens.access_token);
    if (tokens.refresh_token) console.log('REFRESH_TOKEN:', tokens.refresh_token);
    console.log('有効期限     :', tokens.expires_in, '秒');

    if (RESERVE_SESSION_IDS) {
      console.log(`\n=== ${EVENT_ID} のセッションを予約中: ${RESERVE_SESSION_IDS.join(', ')} ===`);
      try {
        const result = await reserveSessions(EVENT_ID, tokens.access_token, RESERVE_SESSION_IDS);

        console.log('\n=== 予約結果 ===');
        console.log(JSON.stringify(result, null, 2));
        // 注意: succeeded/failed のキー名は実APIで要確認（ListSessionsのitemsと同様の前提）
        const succeeded = result.succeeded || [];
        const failed = result.failed || [];
        if (succeeded.length) console.log(`\n成功: ${succeeded.length}件`);
        if (failed.length) {
          console.log(`失敗: ${failed.length}件（理由は上記JSONを参照。未知の理由コードは「拒否」として扱ってください）`);
        }

        const outFile = `reservation-${EVENT_ID}.json`;
        fs.writeFileSync(outFile, JSON.stringify(result, null, 2), 'utf-8');
        console.log(`保存先: ${outFile}`);
      } catch (e) {
        if (e.status === 401) {
          console.error('予約に失敗（401）: トークンが無効です。');
        } else if (e.status === 403) {
          console.error(
            `予約に失敗（403）: このイベント（${EVENT_ID}）に登録されていない可能性があります。イベントの登録サイトで登録してから再実行してください。`
          );
        } else if (e.status === 400) {
          console.error(
            `予約に失敗（400）: リクエスト内容を確認してください（セッションIDは1〜10件・重複不可）: ${e.body}`
          );
        } else {
          console.error('予約に失敗しました:', e.message);
        }
      }
      return;
    }

    console.log(`\n=== ${EVENT_ID} のセッション一覧を取得中 ===`);
    try {
      const { sessions, totalCount } = await fetchAllSessions(EVENT_ID, tokens.access_token, {
        includeAbstracts: INCLUDE_ABSTRACTS,
        locale: LOCALE,
      });

      console.log(`\n取得完了: ${sessions.length}件${totalCount != null ? `（catalog上のtotalCount: ${totalCount}）` : ''}`);

      const outFile = `sessions-${EVENT_ID}.json`;
      fs.writeFileSync(outFile, JSON.stringify(sessions, null, 2), 'utf-8');
      console.log(`保存先: ${outFile}`);

      if (MD_OUTPUT) {
        const mdDir = saveSessionsAsMarkdown(EVENT_ID, sessions);
        console.log(`保存先(Markdown): ${mdDir}/ 配下に${sessions.length}件`);
      }

      console.log('\n--- 先頭5件のタイトル ---');
      sessions.slice(0, 5).forEach((s) => {
        console.log(`- [${s.sessionId ?? '?'}] ${s.title ?? '(タイトルなし)'}`);
      });
    } catch (e) {
      if (e.status === 401) {
        console.error('セッション取得に失敗（401）: トークンが無効です。');
      } else if (e.status === 403) {
        console.error(
          `セッション取得に失敗（403）: このイベント（${EVENT_ID}）に登録されていない可能性があります。イベントの登録サイトで登録してから再実行してください。`
        );
      } else {
        console.error('セッション取得に失敗しました:', e.message);
      }
    }
  } catch (e) {
    console.error('トークン交換に失敗しました:', e.message);
  } finally {
    server.close();
  }
});

server.listen(PORT, () => {
  console.log(`ローカルサーバーを起動しました: ${REDIRECT_URI}`);
  console.log('\n以下のURLをブラウザで開いてBuilder IDでサインインしてください:\n');
  console.log(authUrl.toString());
  console.log('');

  // 可能なら自動でブラウザを開く（失敗しても無視）
  const opener =
    process.platform === 'darwin' ? 'open' : process.platform === 'win32' ? 'start' : 'xdg-open';
  exec(`${opener} "${authUrl.toString()}"`, () => {});
});