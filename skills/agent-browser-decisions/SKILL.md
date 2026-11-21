---
name: agent-browser-decisions
description: Drive web browsers by combining plan files (agent-browser-decisions) with direct agent-browser commands. Prefer this skill over raw agent-browser for web automation — use plan files for multi-step clicks, form filling, multi-field searches, and repeated runs with different inputs, and use agent-browser directly for irreversible submissions, read-only browsing, exploration, 1-2 step one-shot actions, or iframe/Shadow DOM/file uploads. Trigger for "ブラウザで操作して", "フォームに入力して", "サイトで検索して", "何度も同じ操作", "agent-browser", "agent-browser-decisions", "browser automation".
---

# agent-browser-decisions

## Rule

Web 操作は、次の優先順（上が優先）で**手順書（plan JSON）**と **`agent-browser` の直接操作**を使い分ける。

| 優先 | 作業の性質 | 使う手段 |
| --- | --- | --- |
| 1 | 取り消せない送信（決済・投稿・申し込み・削除確定） | **`agent-browser` 直接**（直前までは手順書可、送信だけ1回直接押す） |
| 2 | 読むだけ（`open`/`snapshot`/`get text`/`eval`）、1〜2手の単発操作、画面を見ながら決める探索、iframe / Shadow DOM / ファイルアップロード / 件数カウント | **`agent-browser` 直接** |
| 3 | 入力値を変えて繰り返す作業、または事前確認できる3手以上の定型操作（フォーム入力・複数条件の検索・画面遷移など） | **手順書（`agent-browser-decisions`）** |

- **手順書の3段階解決**: 各手順は ① 観測スナップショット上の互換要素からラベル一致（Unicode 保持・全角半角統一・完全一致→前方一致、または `regex` / `nth`）で1件に絞る（モデルなし）→ ② `nth` 未指定で曖昧または不一致なら判断モデル（OpenAI Decisions API / Gemini Decisions API / TypeSafe Jev / Liquid d1）が観測済みの互換 ref だけから選ぶ（429/5xx/529 は自動で有界リトライ）→ ③ 解決不能・棄権・効果未確認ならその手で停止し、`agent-browser` の直接操作または `--from-step` で引き継ぐ。不一致時に無関係な単一候補へ自動フォールバックはしない。
- **`agent-browser` 直接操作の詳細**: CLI 構文やトラブルシュートが必要なら `agent-browser skills get core` を参照する。

## Setup & Run

前提: `agent-browser`（`npm i -g agent-browser && agent-browser install`）と `uv` が入っていること。第1段階（ラベル一致）だけで通る手順書は API キー不要。第2段階を使う場合だけ対応プロバイダのキー（`OPENAI_API_KEY` / `GEMINI_API_KEY` / `TYPESAFE_API_KEY` / `LIQUID_API_KEY`）を環境変数または `.env` に設定する（未指定時はキーや `--model` から自動判定、`--provider openai|gemini|typesafe|liquid`・`--model`・`--base-url` で明示切替も可。CLI は `--env-file` → `./.env` → リポジトリ直下の `.env` の順で自動読込し、既存環境変数は上書きしない）。

```bash
# グローバル CLI として入れる場合（推奨）
uv tool install git+https://github.com/akitana-airtanker/agent-browser-decisions.git
agent-browser-decisions doctor

# ローカル clone から使う場合
ABD="${AGENT_BROWSER_DECISIONS_DIR:-$HOME/src/agent-browser-decisions}"
git clone https://github.com/akitana-airtanker/agent-browser-decisions.git "$ABD" && cd "$ABD" && uv sync
uv run --project "$ABD" agent-browser-decisions doctor
```

事前に画面のスナップショット（`- button "Login" [ref=e3]` の引用符内が `target`、`button` が `role`）を確かめてから手順書 JSON を書き、検証・実行する（`agent-browser-decisions` が `PATH` にない場合は `uv run --project "$ABD" agent-browser-decisions` に読み替える）:

```bash
URL="https://www.saucedemo.com/"

# 事前確認: 実際のラベルと役割（role）を読み、確認用セッションは閉じる
agent-browser --session inspect open "$URL" && agent-browser --session inspect snapshot -i
agent-browser --session inspect close

cat > /tmp/plan.json <<'EOF'
{
  "url": "https://www.saucedemo.com/",
  "goal": "Log in and open the cart.",
  "reset": true,
  "steps": [
    {"action": "fill", "target": "Username", "value_env": "SAUCEDEMO_USER", "secret": false},
    {"action": "fill", "target": "Password", "value_env": "SAUCEDEMO_PASSWORD"},
    {"action": "click", "target": "Login", "role": "button", "expect": "navigation", "once": true},
    {"action": "click", "target": "Cart", "describe": "the shopping cart icon in the header"}
  ],
  "verify": "location.pathname === '/cart.html'"
}
EOF

agent-browser-decisions validate /tmp/plan.json
SAUCEDEMO_USER=standard_user SAUCEDEMO_PASSWORD=secret_sauce agent-browser-decisions run /tmp/plan.json
```

途中から既存セッションで再開するときは `run /tmp/plan.json --session <session> --from-step <N>` を使う。タイムアウト調整は `--timeout <秒>`（ブラウザ）/ `--api-timeout <秒>`（API）。

## Plan schema

`validate` は未定義キー・型誤り・不正な組み合わせを実行前に `rc 2` で弾く（JSON Schema は `schema/plan.schema.json`）。

- トップレベル:
  - `steps`（必須・非空配列）, `url`（`reset: true` では必須）, `goal`, `verify`（JS 式文字列）, `reset`（boolean）, `verify_timeout_s`（0 以上の有限数値、既定 `10`）, `$schema`（任意）
- 各手順:
  - `action`（必須: `click` | `fill` | `select` | `check` | `press`）
  - `target`（`press` 以外は必須、`press` では指定不可）
  - `value`（`fill` / `select` / `press` で必須。ただし `fill` は `value_env` で代替可。`click` / `check` では指定不可）
  - `value_env`（`fill` 専用。実行時に環境変数から値を読む。未設定ならブラウザ起動前に `rc 2` で失敗）
  - `secret`（`fill` 専用 boolean。`true` でエラー出力から値を `[REDACTED]` に伏せる。`value_env` 使用時は既定 `true`）
  - `role`（任意・`action` と互換な ARIA role のみ、`press` では不可）
  - `nth`（1 以上の整数。`decide: true` と併用不可）
  - `match`（`"regex"` のみ。通常一致では書かない。`decide: true` と併用不可）
  - `describe`（判断モデルへ渡す説明文）
  - `expect`（`"navigation"` のみ）
  - `navigates`（boolean。URL 遷移しない `link` で `false` を指定）
  - `once`（boolean。`true` で再試行と Enter フォールバックを無効化し最大1回実行）
  - `decide`（boolean。`true` で第1段階を飛ばし第2段階へ直接送る。`nth` / `match` と併用不可）

## Plan rules

- **機密値は `value_env` を使う**: パスワード・API キー・トークンは手順書 JSON に平文で書かず、`value_env` で環境変数から渡す。
- **1手順 = 1操作**: `target` は `snapshot -i` で読んだ引用符内のラベルをそのまま書く。短縮しすぎると前方一致で別ボタン（例: `"Log in"` → `"Log in with Google"`）に当たることがある。
- **動的に開く部品と `role`**: カレンダーや候補リストなど操作の途中で開く部品は、可能なら事前に1度開いて確認する。`role` はスナップショットで実際に確認した要素にだけ書く（推測で書くと3秒待ってから解除されるか、正しい要素が候補から外れる）。未確認のまま書く手順は `role` を省き `describe` を添える。
- **順番と同一ラベル**: 「3番目の結果」のような順番指定は判断モデルに数えさせず `nth` を使う（例: `{"action": "click", "target": "^\\d+\\s*comments?$", "match": "regex", "nth": 3}`）。各商品の「Add to cart」のように項目名を含まない同一ラベルが並ぶとき、順番が固定なら `nth`、並び順が変わりうるなら `agent-browser` の直接操作に切り替える。既存手順書の再実行前も `nth` の位置と対象が一致しているか確かめる。
- **遷移・副作用・`select`**:
  - URL が変わらない `link`（SPA のタブ切替やモーダルを開くリンク）は `"navigates": false` を付ける（付けないと URL 変化を待って Enter を押し、`wrong_target` で止まる）。
  - `"expect": "navigation"` は必ずページ移動が起きる手順にだけ付ける。ログイン失敗など移動しない分岐があるときは付けず、`verify` で判定する。
  - 支払い・投稿・申し込み・削除確定など取り消せない送信は手順書に入れず、直前で止めて直接操作で1回だけ押す。それ以外でも応答が遅く二重実行を避けたい手順には `"once": true` を付ける。
  - `select` の反映確認は未検査（常に `ok`）なので、選択結果が重要なら `verify` で `.value` や表示テキストを検査する。
- **`verify` の書き方**: `verify` には JSON 化できる JS 式を書く。真になるプリミティブ、`{}`/`[]`、または `{"passed": true}` が合格。`undefined`・`NaN`・構文エラー・非ゼロ終了・`{"passed": false}` は不合格。`verify` を省くと `verified: null` のまま全手順完了で `ok: true` になるため、最終状態を保証したいときは必ず書く。入力欄は `innerText` ではなく `.value` で調べる。
- **`reset` と `--session`**: `"reset": true` は `url` 必須で、パスワード管理を切った新しい独立セッションを起動し、成功時・中断時に自動で閉じる（`--session` とは併用不可）。ログイン済みの既存セッションを引き継ぐときは `"reset": false`（または省略）＋ `--session <名前>` を渡す。`url` があると既存セッションでも開き直し、開いていたダイアログを閉じるため、今の画面から続けるときは `url` を省く。

## Result, exit codes & direct takeover

| Exit | 意味 | JSON の主なフィールド |
| --- | --- | --- |
| `0` | 全手順が実行・確認され、`verify` も合格（または未指定） | `ok: true`, `verified: true \| null`, `session_open: false`（`reset` 時） |
| `1` | 手順失敗・`verify` 不合格・`open`/タイムアウト/中断等の実行時エラー | `ok: false`, `stopped_at_step`（`0` は開始前）, `session`, `session_open`, `error`, `steps[]`（`chose`, `effect`, `reason`） |
| `2` | スキーマ違反・オプション矛盾（`reset: true` + `--session` 等）・`value_env` 未設定 | `ok: false`, `problems: [...]`（ブラウザ未操作） |

- **引き継ぎと途中再開**: 失敗時（`session_open: true`）は結果の `session` にブラウザが残っている。`stopped_at_step` と `steps[]` の `chose` / `effect` を確認し、次のどちらかで続ける。
  - 直接操作: `agent-browser --session <session> snapshot -i` → `click @eN` … → 終わったら `agent-browser --session <session> close`。
  - 途中再開: 手順書を直し、`agent-browser-decisions run /tmp/plan.json --session <session> --from-step <stopped_at_step>` で実行する（同じ手順書を step 1 から丸ごと再実行しない。済んだ手順の二重実行や選択済み要素での停止を避けるため）。
- **ダイアログ・リンク・入力の注意点**:
  - Cookie 同意などの重なりで `Element is covered by ...` になったら、閉じた後に必ず `snapshot -i` を取り直す（再描画で `@eN` が変わる）。`href="javascript:void(0)"` のリンクは `open` せずクリックする。
  - 原稿どおりの文を入れる欄では、1回入力したら表示値（リッチテキストでは本文）を読み戻して照合する。不一致でも繰り返し入力せず（追記重複の原因）、実際に編集可能な要素を調べ直す。
- **直接操作でのログインとプロファイル**:
  - ログインを伴う直接操作では、パスワード管理のポップアップが操作を奪うのを防ぐため、`PROFILE="$(agent-browser-decisions profile)"` で作った一時プロファイルを `--profile "$PROFILE"` に渡す（セッション終了後に `rm -rf "$PROFILE"` で掃除する）。
  - 普段のブラウザのログイン状態（`--profile Default`）は OS の保護やパス差異で読めないことが多い。最初から `--headed` を付けて開き、ログインは利用者に画面で行ってもらう（`--headed` と `--profile "$PROFILE"` は併用できる）。
- **後片付け**: 自分が開いた `--session` だけを閉じる。`close --all` は他の作業のセッションも閉じるため使わない。
