---
name: agent-browser-decisions
description: Single entry point for web browser automation combining agent-browser-decisions plans (with automatic internal batch --bail fusion) and direct agent-browser commands. Prefer this skill over raw agent-browser — use plan files (click/fill/select/check/press/upload, including auto-inlined iframes and open/closed Shadow DOM; `verify` cannot inspect closed Shadow DOM internals) for multi-step forms, searches, and repeated runs with different inputs, and use agent-browser directly for irreversible submissions, read-only browsing, exploration, 1-2 step one-shot actions, or explicit cross-origin frame switching. Trigger for "ブラウザで操作して", "フォームに入力して", "サイトで検索して", "何度も同じ操作", "agent-browser", "agent-browser-decisions", "browser automation".
---

# agent-browser-decisions

## Rule

Web 操作は、次の優先順（上が優先）で**手順書（plan JSON）**と **`agent-browser` の直接操作**を使い分ける。

| 優先 | 作業の性質 | 使う手段 |
| --- | --- | --- |
| 1 | 取り消せない送信（決済・投稿・申し込み・削除確定） | **`agent-browser` 直接**（直前までは手順書可、送信前に利用者へ確認して1回だけ直接押す。カード決済の入力と確定は利用者が普段のブラウザで行う） |
| 2 | 読むだけ（`open`/`snapshot`/`get text`/`eval`）、1〜2手の単発操作、画面を見ながら決める探索、明示的な `frame` 切替が必要な深い・クロスオリジン iframe や AXTree に出ない独自要素、件数カウント | **`agent-browser` 直接** |
| 3 | 入力値を変えて繰り返す作業、または事前確認できる3手以上の定型操作（フォーム入力・ファイル添付・複数条件の検索・画面遷移、自動インライン展開される iframe や open/closed Shadow DOM 内の操作要素を含む。静的フォームは内部で自動的に `batch --bail` 化） | **手順書（`agent-browser-decisions`）** |

- **手順書の3段階解決（同一画面フォームは自動バッチ化）**: 各手順は ① 観測スナップショット上の互換要素からラベル一致（Unicode 保持・全角半角統一・既定は完全一致のみ、明示指定で `prefix` / `regex` / `nth`）で1件に絞る（モデルなし。連続する同一画面の `fill`/`check`/`select` が第1段階で一意に決まるときは内部で `agent-browser batch --bail` にまとめて高速実行し、部分失敗時は先頭から連続して確認できた区間だけ確定し、1手以上進んで残りが条件を満たせば再バッチ化、0件なら先頭未確認手から個別実行へ戻す）→ ② `--decisions` 等で明示有効化時のみ（既定オフ）、`nth` 未指定で曖昧または不一致なら判断モデル（OpenAI / OpenRouter / Gemini / TypeSafe Jev / Liquid d1）が観測済みの互換 ref だけから選ぶ（429/5xx/529 は自動で有界リトライ）→ ③ 解決不能・棄権・効果未確認ならその手で停止し、`agent-browser` の直接操作または `--from-step` で引き継ぐ。不一致時に無関係な単一候補へ自動フォールバックはしない。
- **`agent-browser` 直接操作の詳細**: CLI 構文やトラブルシュートが必要なら `agent-browser skills get core` を参照する。

## Setup & Run

前提: `agent-browser`（`npm i -g agent-browser && agent-browser install`）と `uv` が入っていること。第1段階（ラベル一致）だけで通る手順書は API キー不要。第2段階を使う場合だけ対応プロバイダのキー（`OPENAI_API_KEY` / `OPENROUTER_API_KEY` / `GEMINI_API_KEY` / `TYPESAFE_API_KEY` / `LIQUID_API_KEY`）を環境変数または `.env` に設定する（未指定時はキーや `--model` から自動判定、`vendor/model` 形式は OpenRouter へ自動振分。`--provider openai|openrouter|gemini|typesafe|liquid`・`--model`・`--base-url` で明示切替も可。CLI は `--env-file` / 環境変数 `AGENT_BROWSER_DECISIONS_ENV_FILE` 明示時はそのファイルのみ（全キー許可、未存在でも `./.env` へフォールバックしない）、未指定時のみ `./.env` → リポジトリ直下の `.env` の順で読込し、既存環境変数は上書きしない。暗黙の `.env` 読込は API キー・`DECISION_PROVIDER`・`DECISION_MODEL` のみに限定され、`AGENT_BROWSER_*` や `*_BASE_URL` は読み込まない）。

```bash
# グローバル CLI として入れる場合（推奨）
uv tool install git+https://github.com/akitana-airtanker/agent-browser-decisions.git
agent-browser-decisions doctor

# ローカル clone から使う場合
ABD="${AGENT_BROWSER_DECISIONS_DIR:-$HOME/src/agent-browser-decisions}"
git clone https://github.com/akitana-airtanker/agent-browser-decisions.git "$ABD" && cd "$ABD" && uv sync
uv run --project "$ABD" agent-browser-decisions doctor
```

**高速実行の基本手順（初回は確認用セッションを閉じずに再利用し、2回目以降は `--var` で1行実行する）**:
- `doctor` はエラー時だけ呼べばよい（正常時は不要）。`run` はブラウザ操作前に自動でスキーマ検証するため、通常実行前に別コマンドで `validate` を呼ぶ必要はない。
- 事前確認で開いたセッション `$S` は**閉じずに残し**、`--session "$S" --no-open --expect-url "$URL" --close` でそのまま `run` する（Chrome の再起動とページ再読込を省き、成功時はクローズを試み、失敗時だけ開いたまま残る）。
- **フォームのすべての入力値・選択対象（テキスト欄だけでなくラジオ・チェックボックスのラベルも含む）をトップレベル `vars` と `{{KEY}}` に出し、`verify` も `VARS.*` で汎用的に照合**して保存する（これにより2回目にどの項目が変わっても JSON を書き直さず `--var KEY=VALUE` の1行だけで再実行できる）。
- `--session "$S" --no-open --close` の結果で `ok: true`・`verified: true`・`session_open: false` が返っていれば、全項目の反映確認と同一レジストリでのセッション終了確認まで完了済み。一時プロファイルを手動で消す場合は、`close` の終了コード `0` と同一レジストリの `session info --json`・`session list --json` で終了を確認してから削除する。

```bash
URL="https://httpbin.org/forms/post"
S="probe-$$"

# 1. 事前確認: ラベルと role を読む（セッション $S は閉じずにそのまま残す）
agent-browser --session "$S" open "$URL" && agent-browser --session "$S" snapshot -i

# 2. 手順書を書き、開いている $S 上で --no-open --close で即実行（成功時のみ自動クローズ）
TMPDIR_PLAN="$(mktemp -d /tmp/plan.XXXXXX)" && \
cat > "$TMPDIR_PLAN/plan.json" <<'EOF'
{
  "url": "https://httpbin.org/forms/post",
  "reset": true,
  "vars": {"name": "Taro Yamada", "size": "Medium"},
  "steps": [
    {"action": "fill", "target": "Customer name:", "value": "{{name}}"},
    {"action": "check", "target": "{{size}}", "role": "radio"}
  ],
  "verify": "document.querySelector('input[name=custname]').value === VARS.name"
}
EOF
agent-browser-decisions run "$TMPDIR_PLAN/plan.json" --session "$S" --no-open --expect-url "$URL" --close

# 3. 2回目以降（値だけ変更）: JSON を書き直さず --var だけで1コマンド実行（完了後に一時ディレクトリ削除）
agent-browser-decisions run "$TMPDIR_PLAN/plan.json" --var name="Hanako Sato" --var size="Large" && rm -rf "$TMPDIR_PLAN"
```

機密値（ログインパスワード等）は `vars` ではなく `value_env`（例: `{"action": "fill", "target": "Password", "value_env": "APP_PASSWORD"}`）で環境変数から渡す。途中から既存セッションで再開するときは、停止手（バッチ区間だった場合は後続手も含む）の反映状態を確認したうえで `run "$TMPDIR_PLAN/plan.json" --session <session> --from-step <N> --expect-url "<intended-url>"` を使う（ログイン済み・副作用操作では再開前に差分と再開番号を利用者へ提示して承認を得る）。タイムアウト調整は `--timeout <秒>`（ブラウザ）/ `--api-timeout <秒>`（API）。

## Plan storage, reuse & update

手順書を毎回使い捨てにせず、スキル側の運用ルールとして「信頼できる場所から探す → 再利用する → 復旧後に確認して更新する」を管理する（CLI 本体の自動探索機能ではない）。

- **保存先の優先順**:
  1. 利用者がパスを指定した場合はそのパスを使う。
  2. 個人用の繰り返し操作は `~/.local/share/agent-browser-decisions/plans/<site>-<task>.json`（ディレクトリが無ければ作成）。
  3. チームで共有・Git 管理する定型操作は、信頼できるプロジェクト内の `plans/<site>-<task>.json`（既存の `plans/` または `.agent-browser/plans/` があればそこへ揃える）。
  4. 再利用しない1回限りの作業だけ `mktemp -d` で作った一時ディレクトリ内の `plan.json` を使う。
- **実行前の探索と信頼境界**:
  - 自動探索の対象は **個人用ディレクトリ（`~/.local/share/agent-browser-decisions/plans/`）または利用者が明示したパス** に限る。
  - リポジトリ内 `plans/`（`.agent-browser/plans/`）は信頼できる自前・チーム管理リポジトリに限り、**`verify`（ページ内で任意 JS を評価）と `steps` を確認したうえで実行**する。外部から clone しただけの未信頼リポジトリの `plans/` は自動探索・自動実行しない（ログイン済み永続プロファイル併用時は、初回作成した手順書を含め `steps` と `verify` を実行前に利用者へ提示して承認を得る）。
  - 対象 URL・`goal` に該当する候補が複数あるときは勝手に1つを選ばず、一覧を示して利用者に確認する。
- **復旧と更新確認（再開3原則）**: 既存手順書が UI ラベル変化等で止まったら、停止時の `url` を機械的に `--expect-url` へ流用せず、**元の手順書や利用者が想定する URL プレフィックス**を指定して再開する（誤遷移の追認を防ぐ）。再開前に `steps[]` と `stopped_snapshot`（必要なら読み戻し）で停止手（バッチ区間だった場合は後続手も含む）の反映状態を確認し、**その位置以降の全手を実行してよいと確認できた場合に限り、未反映なら `stopped_at_step`、すでに反映済みで残りの手があるなら `stopped_at_step + 1` から再開する（バッチ部分失敗で `N` 手目が未反映・`N+1` 手目が反映済みのように飛び飛びの場合は `--from-step N` を使わず、未反映手だけの直接操作または絞り込んだ手順書へ切り替える。最終手が反映済み、または `verify` のみ失敗で `stopped_at_step: null` なら再実行せず完了確認のみ行って閉じる）。副作用の有無が判別できない場合は自動で再試行せず、利用者へ報告して判断を仰ぐ**。ログイン済み画面や副作用を伴う操作では、手順書の差分と再開番号（`--from-step N`）を**再開前に利用者へ提示して確認**してから `--session <session> --from-step <N> --expect-url "<intended-url>" --close` を実行する。完走（`ok: true`・`verified: true`）後、**リポジトリ共有の `plans/` への書き戻しは必ず差分を提示して利用者確認を得てから上書き**する（個人用保存先でも変更点を報告して更新する）。
- **保存時の衛生ルール**: パスワード・トークン・機密の個人情報は保存手順書に平文で書かず `value_env` にする。可変値は `vars` / `{{KEY}}` / `VARS.*` に切り出して再利用できる形で残す。

## Plan schema

`validate` は Python 実装の `validate_plan` で未定義キー・型誤り・不正な組み合わせを実行前に `rc 2` で弾く（対応する JSON Schema は `schema/plan.schema.json`）。

- トップレベル:
  - `steps`（必須・非空配列）, `url`（`reset: true` では必須）, `goal`, `vars`（`string -> string` の既定変数マップ。`url`/`goal`/`target`/`value`/`describe` の `{{KEY}}` を展開し、`verify` 内で `VARS.KEY` として参照可。CLI の `--var KEY=VALUE` で上書き）, `verify`（JS 式文字列）, `reset`（boolean）, `settle_s`（0 以上の数値）, `verify_timeout_s`（0 以上の有限数値、既定 `10`）, `$schema`（任意）
- 各手順:
  - `action`（必須: `click` | `fill` | `select` | `check` | `press` | `upload`）
  - `target`（`press` 以外は必須、`press` では指定不可）
  - `value`（`fill` / `select` / `press` / `upload` で必須。`upload` はローカルファイルパス。`fill` のみ `value_env` で代替可。`click` / `check` では指定不可）
  - `value_env`（`fill` 専用。実行時に環境変数から値を読む。未設定ならブラウザ起動前に `rc 2` で失敗）
  - `secret`（`fill` 専用 boolean。`true` でエラー出力・`stopped_snapshot`・`verify_value`・`url`・`steps[].chose`・Tier 2 候補ペイロードから既知文字列を `[REDACTED]` に伏せる。`value_env` 使用時は既定 `true`）
  - `role`（任意・`action` と互換な ARIA role のみ、`press` では不可）
  - `nth`（1 以上の整数。`decide: true` と併用不可）
  - `match`（`"exact"`（既定・省略可）/ `"prefix"`（末尾に動的状態が付くラベル向け）/ `"regex"`。`decide: true` と併用不可）
  - `describe`（判断モデルへ渡す説明文）
  - `expect`（`"navigation"` のみ。`click` / `press` / `upload` が対象で `fill` / `check` / `select` では無視される）
  - `navigates`（boolean。URL 遷移しない `link` で `false` を指定）
  - `once`（boolean。`true` で全段階の再試行を無効化し最大1回実行。`retry: true` と併用不可）
  - `retry`（boolean。`press` では指定不可。`click`/`upload` は既定 `false` で同一要素への再試行・リンク `Enter` 補完を行わない。折り返しリンク等で再試行を許可したいときだけ `true` を明示する。`fill`/`check`/`select` は既定 `true`）
  - `decide`（boolean。`true` で第1段階を飛ばし第2段階へ直接送る。`nth` / `match` と併用不可。実行時に `--decisions` 等で第2段階を有効化する必要あり）
  - `timeout_s`（0 以上の数値。`press` 以外で要素出現・安定待ちの上限秒をステップ単位で上書き。計画全体の `settle_s` や CLI `--settle-s` で一括指定も可能。`0` や `0.5` で早めに諦めて第3段階へ返せる）

## Plan rules

- **第2段階（Decisions API）は既定オフ・有効化前に利用者承認**: `run` は既定で第1段階＋第3段階のみで動作し、環境変数に API キーがあっても外部 API へ URL・候補名を送らない。有効化すると URL・トップレベル `goal`・step の `describe`（未指定時は `target`）・候補ラベル・可視入力値・要素属性・周辺文脈が外部 API へ送信され、既知の `value_env` 秘密値以外は伏字されないため**画面由来の未知の秘密値や個人情報も送信され得る**。`--decisions` / `--provider` / `--model` / `--base-url` / `--mode decide` を付ける場合は、**送信され得る画面内容と送信先プロバイダ／エンドポイントを確認したうえで必ず事前に利用者の承認を得る**。
- **機密値は `value_env` を使う**: パスワード・API キー・トークンは手順書 JSON に平文で書かず、`value_env` で環境変数から渡す。
- **1手順 = 1操作**: `target` は `snapshot -i` で読んだ引用符内のラベルをそのまま書く（既定は正規化後の完全一致のみ。元の要素が消えて正規化後が同形になる別要素だけが残る画面を除き、別ラベルへは一致しない）。ARIA ラベル末尾に現在の選択値などが付く UI（例: `"Change ticket type. Round trip"`）だけ、意図的に `"match": "prefix"` を付ける。
- **動的に開く部品は手前で止めて段階作成する**: カレンダーやモーダルなど開始画面に見えない部品がある場合は、推測で全手順を一発書きせず、開くクリックまでを書いて `--keep-open` で実行し、開いた画面の `snapshot -i` を確認してから後半を手順書へ追記して `--session <id> --from-step N --expect-url "<intended-url>" --close` で仕上げる（ログイン済み・副作用操作では実行前に差分と `verify` を利用者へ提示して確認する）。静的画面や初回検証でラベル不一致を早く検知したいときは `--settle-s 0.5`（または `0`）で早めに諦めて引き継ぐ。`role` はスナップショットで確認済みの要素にだけ書く。
- **順番と同一ラベル**: 「3番目の結果」のような順番指定は判断モデルに数えさせず `nth` を使う（例: `{"action": "click", "target": "^\\d+\\s*comments?$", "match": "regex", "nth": 3}`）。各商品の「Add to cart」のように項目名を含まない同一ラベルが並ぶとき、順番が固定なら `nth`、並び順が変わりうるなら `agent-browser` の直接操作に切り替える。既存手順書の再実行前も `nth` の位置と対象が一致しているか確かめる。
- **遷移・副作用・`select`**:
  - URL が変わらない `link`（SPA のタブ切替やモーダルを開くリンク）は `"navigates": false` を付ける（付けないと URL 変化が起きず `wrong_target` で止まる。既定では `Enter` 補完や再クリックは行わない）。
  - `"expect": "navigation"` は必ずページ移動が起きる手順にだけ付ける。ログイン失敗など移動しない分岐があるときは付けず、`verify` で判定する。
  - 支払い・投稿・申し込み・削除確定など取り消せない送信は手順書に入れず、直前で止めて利用者へ確認してから直接操作で1回だけ押す。それ以外でも応答が遅く二重実行を避けたい手順には `"once": true` を付ける。
  - カード決済（Stripe など）は決済画面の手前まで案内し、カード情報の入力と確定は利用者が普段使っているブラウザで行うよう最初から伝える。自動操作のブラウザ（一時プロファイルの Chromium など）では、不正検知により理由を示さずに拒否されることがある（例:「この購入を完了できません」）。
  - カード番号などの機密値が画面に出ているあいだは `screenshot` も `snapshot` も取らない。`secret` と `value_env` で伏せられるのは手順書が知っている文字列だけで、画像は伏せられない。取った画像や出力は、保存先のファイルや会話のログに残る。
  - `select` の反映確認は未検査（常に `ok`）なので、選択結果が重要なら `verify` で `.value` や表示テキストを検査する。
- **`verify` の書き方**: `verify` には JSON 化できる JS 式を書く。真になるプリミティブ、`{}`/`[]`、または `{"passed": true}` が合格。`undefined`・`NaN`・構文エラー・非ゼロ終了・`{"passed": false}` は不合格。`verify` を省くと `verified: null` のまま全手順完了で `ok: true` になるため、最終状態を保証したいときは必ず書く。入力欄は `innerText` ではなく `.value` で調べる。
- **`reset` と `--session` / `--no-open` / `--reload` / `--keep-open` / `--close`**: `"reset": true` は `url` 必須で、単独実行時にパスワード管理を切った新しい独立セッションを起動し、成功時・中断時に自動で閉じる。事前確認で開いた既存セッションを再利用するときは `--session <名前> --no-open --close` を渡す（`reset: true` のままでも再遷移せず、成功時だけ自動クローズし失敗時は開いたまま残す）。同じサイトを複数回連続で回すときは、初回に `--keep-open` でセッションを残し、2回目以降を `--session <名前> --reload --var ...`（フォーム初期化あり・Chrome 再起動なし）で回して最後だけ `--close` を付けると大幅に速い。

## Result, exit codes & direct takeover

| Exit | 意味 | JSON の主なフィールド |
| --- | --- | --- |
| `0` | 全手順が実行・確認され、`verify` も合格（または未指定） | `ok: true`, `verified: true \| null`, `session_open: false`（`reset` 時） |
| `1` | 手順失敗・`verify` 不合格・`open`/タイムアウト/中断等の実行時エラー | `ok: false`, `stopped_at_step`（`0` は開始前）, `session`, `session_open`, `stopped_snapshot`（停止時点の観測スナップショット）, `error`, `steps[]`（`chose`, `effect`, `reason`） |
| `2` | スキーマ違反・オプション矛盾（`reset: true` + `--session` 等）・`value_env` 未設定 | `ok: false`, `problems: [...]`（ブラウザ未操作） |

- **引き継ぎと反例駆動の手順書精密化（CEGAR ループ）**: 失敗時（`session_open: true`）は結果の `session` にブラウザが残っており、観測済みの場合は **`stopped_snapshot`**（停止時点の実スナップショット。既知の秘密値は伏字されるが、画面由来の未知の秘密や非機密本文は残るため無条件に共有しない）が JSON に同梱される。`stopped_at_step`・`stopped_snapshot`・`steps[]` をそのまま反例として読み、次のどちらかで続ける。
  - 途中再開と手順書の更新（定型化したい場合）: 上記「復旧と更新確認（再開3原則）」に従い、まず `steps[]` と `stopped_snapshot`（必要なら読み戻し）で停止手（バッチ区間だった場合は後続手も含む）の反映状態を確認し、**その位置以降の全手を実行してよいと確認できた場合に限り、未反映なら `stopped_at_step`、すでに反映済みで残りの手があるなら `stopped_at_step + 1` から再開する（バッチ部分失敗で未反映と反映済みが飛び飛びの場合は `--from-step` を使わず、未反映手だけの直接操作または絞り込んだ手順書へ切り替える。最終手が反映済み、または `verify` のみ失敗で `stopped_at_step: null` なら再実行せず完了確認のみ行って閉じる）。副作用の有無が判別できない場合は自動で再試行せず利用者へ確認する**。`--expect-url` には停止結果の `url` を機械的に流用せず（誤遷移を追認するため）、**元の手順書や利用者が想定する URL プレフィックス**を指定する。ログイン済み画面や副作用を伴う操作で手順書を修正・再開する場合は、差分と再開位置（`--from-step N`）を**再開前に利用者へ提示して確認**してから `agent-browser-decisions run <plan.json> --session <session> --from-step <N> --expect-url "<intended-url>" --close` を実行する（完走後の共有 `plans/` 上書きも差分確認のうえで行う）。なお `reset: true` の失敗後または `--keep-open` 維持後に別コマンドで閉じた場合、起動時に記録した一時 `profile` パスが残るため、不要なら `agent-browser --session <session> close` 成功後に同一レジストリの `session info --json`（`active: false`・`socketDir` 読取可）と `session list --json` で終了を確認してから `rm -rf` で掃除する。
  - 直接操作: 1〜2手の例外処理でも、**実行前に最新の `agent-browser --session <session> snapshot -i` で実画面と最新 ref を確認し、対象手が未反映かつ副作用の重複がないことを確かめてから**（`stopped_snapshot` は再観測失敗時に古い場合があるため ref をそのまま流用せず、ログイン済み・副作用のある画面では利用者確認のうえ）直接 `click @eN` などを実行する。直接操作の後に残り手を `--from-step $((N+1))` で再開する場合も、再開3原則に従い `N+1` 以降の全手が未反映かつ実行可能と確認できた場合だけ使う（部分バッチで後続手が反映済みなら未反映手だけの直接操作または絞り込んだ手順書へ切り替える）。終わったら `agent-browser --session <session> close` の成功と同一レジストリ（`session info --json`＋`session list --json`）での終了を確認してから、起動時に記録した一時 `profile` のみを `rm -rf` で掃除する。
- **ダイアログ・リンク・入力の注意点**:
  - Cookie 同意などの重なりで `Element is covered by ...` になったら、閉じた後に必ず `snapshot -i` を取り直す（再描画で `@eN` が変わる）。`href="javascript:void(0)"` のリンクは `open` せずクリックする。
  - 原稿どおりの文を入れる欄では、1回入力したら表示値（リッチテキストでは本文）を読み戻して照合する。不一致でも繰り返し入力せず（追記重複の原因）、実際に編集可能な要素を調べ直す。
- **直接操作でのログインとプロファイル**:
  - ログインを伴う直接操作では、パスワード管理のポップアップが操作を奪うのを防ぐため、`PROFILE="$(agent-browser-decisions profile)"` で作った一時プロファイルを `--profile "$PROFILE"` に渡す（`agent-browser --session <id> close` が終了コード `0` を返し、同一レジストリの `session info --json` と `session list --json` で終了を確認してから `rm -rf "$PROFILE"` で掃除する）。
  - 普段のブラウザのログイン状態（`--profile Default`）は OS の保護やパス差異で読めないことが多い。最初から `--headed` を付けて開き、ログインは利用者に画面で行ってもらう（`--headed` と `--profile "$PROFILE"` は併用できる）。
- **後片付け**: 自分が開いた `--session` だけを閉じる。`close --all` は他の作業のセッションも閉じるため使わない。
