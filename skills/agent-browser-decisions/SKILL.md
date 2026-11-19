---
name: agent-browser-decisions
description: Drive web browsers by combining plan files (agent-browser-decisions) with direct agent-browser commands. Prefer this skill over raw agent-browser for web automation — use plan files for multi-step clicks, form filling, searches, and repeated runs with different inputs, and use agent-browser directly for read-only browsing, exploration, one-shot checks, or iframe/Shadow DOM/file uploads. Trigger for "ブラウザで操作して", "フォームに入力して", "サイトで検索して", "何度も同じ操作", "agent-browser", "agent-browser-decisions", "browser automation".
---

# agent-browser-decisions

## Rule

ブラウザの Web 操作は、作業の性質に応じて**手順書（plan JSON）での実行**と **`agent-browser` の直接操作**を使い分ける。

- **手順書で `agent-browser-decisions` を実行する作業**: 複数ステップのクリック・入力・選択・検索、事前にスナップショットで要素名を確かめられる作業、入力値を変えて繰り返す作業。各手順はまず名前の一致で要素を決め、決まらなければ OpenAI Decisions API が観測済みの要素から選び、それでも止まれば `agent-browser` の直接操作へ引き継ぐ。最後に `verify` で結果を確かめる。
- **`agent-browser` で直接行う作業**: 情報を読むだけ・URL を開くだけ（`open`, `snapshot`, `get text`, `eval`）、1〜2手の単発操作、遷移先を読みながら次の行動を決める探索作業、iframe・Shadow DOM・ファイルアップロード・数を数える操作。詳しい CLI 構文やトラブルシュートが必要なら `agent-browser skills get core` を参照する。

## Run

前提: `agent-browser`（`npm i -g agent-browser && agent-browser install`）と `uv` が入っていること。未導入なら先にリポジトリを取得する。名前の一致（第1段階）だけで完結する手順書は `OPENAI_API_KEY` 不要。第2段階（Decisions API）を使う場合だけ `.env` にキーを設定する。

```bash
ABD="${AGENT_BROWSER_DECISIONS_DIR:-$HOME/src/agent-browser-decisions}"
git clone https://github.com/akitana-airtanker/agent-browser-decisions.git "$ABD" && cd "$ABD" && uv sync && cp .env.example .env
```

画面を確かめてから手順書 JSON を書き、検証・実行する:

```bash
ABD="${AGENT_BROWSER_DECISIONS_DIR:-$HOME/src/agent-browser-decisions}"
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
    {"action": "fill", "target": "Username", "value": "standard_user"},
    {"action": "fill", "target": "Password", "value": "secret_sauce"},
    {"action": "click", "target": "Login", "role": "button", "expect": "navigation"},
    {"action": "click", "target": "Cart", "describe": "the shopping cart icon in the header"}
  ],
  "verify": "location.pathname === '/cart.html'"
}
EOF

# .env がある場合だけ --env-file を渡す（名前の一致だけで通るなら不要）
ENV_ARGS=()
[ -f "$ABD/.env" ] && ENV_ARGS=(--env-file "$ABD/.env")
uv run --project "$ABD" "${ENV_ARGS[@]}" agent-browser-decisions validate /tmp/plan.json
uv run --project "$ABD" "${ENV_ARGS[@]}" agent-browser-decisions run /tmp/plan.json
```

## Plan schema

`validate` は未定義キーや未対応の値を実行前に弾く。使えるキーは次のとおり。

- トップレベル: `url`, `goal`, `steps`（必須）, `verify`, `reset`, `verify_timeout_s`
- 各手順: `action`（`click` / `fill` / `select` / `check` / `press`）, `target`（`press` 以外は必須）, `value`（`fill` / `select` / `press` で必須。`press` はキー名）, `role`, `nth`（1以上の整数）, `match`（`"regex"` のみ対応。通常の一致では `match` キーを書かない）, `describe`, `expect`（`"navigation"` のみ対応）, `decide`（`true` で名前の一致を飛ばす）

## Plan rules

- 1手順 = 1操作。`target` は事前に取ったスナップショットで読んだラベルをそのまま書く。
- カレンダーや候補リストのように操作の途中で開く部品は、開始ページのスナップショットだけでは手順やラベルが分からないことが多い。可能なら手順書を書く前に1度開いて確かめる。未確認のまま書く場合は `role` を省き、`describe` に人が読む説明を添える（止まったらその手から直接操作で引き継ぐ）。
- `role`（`button`, `link`, `textbox` などの要素の役割）はスナップショットで実際に見た要素にだけ書く。推測で書くと正しい要素が候補から外れる。
- 「3番目の結果」のような順番は `"match": "regex", "nth": 3` で指定する。判断モデルに数えさせない。
- 同じラベルが項目ごとに並ぶ（各商品の「Add to cart」など）とき、スナップショットに項目名が含まれないと `describe` でも見分けられない。順番が確かなら `nth` を使い、動的に変わるならその手順は `agent-browser`（DOM 検査や座標・セレクタ指定）で直接行う。ランキングのように並び順が変わりうる一覧で既存の手順書を再実行するときは、`nth` の位置と `describe` の固有名が今の画面と一致しているか先に確かめる。
- `"expect": "navigation"` は、結果にかかわらず必ずページ移動が起きる手順にだけ付ける。ログインの失敗のように移動しない場合がある手順には付けず、結果は `verify` で判定する。
- 支払い、投稿、申し込みのように取り消せない送信の手は手順書に入れない。反映確認で「効いていない」と判断した手は押し直すため、応答の遅い送信が二重になることがある。手順書はその手前で止め、送信は `agent-browser` の直接操作で1回だけ行う。
- `verify` は URL や DOM で目的の状態を直接確かめる JavaScript 式を書く。`ok: true` が保証するのは「全手順が実行され、`verify` の式が真を返したこと」だけなので、途中の誤選択をすり抜けさせないよう、最終状態を具体的に検査する。入力欄の値は `innerText` に出ないので、`input` と `textarea` は `.value` で調べる。
- `"reset": true` はパスワード管理を切った新しい独立セッションで実行し、成功時は自動で閉じる。すでにログイン済みの既存セッションを引き継いで実行したいときは `"reset": false`（または省略）にし、`run /tmp/plan.json --session <既存セッション名>` を渡す。手順書に `url` があると、`run` は `--session` を渡しても最初にその URL を開き直し、開いていたダイアログは閉じる。開いた状態から続けるときは `url` を省く。

## Result & direct takeover

- `ok: true` は全手順の実行と `verify` の両方が通ったときだけ。失敗時は `stopped_at_step` と、結果の `steps[]` にある各手順の `chose` / `effect` を見て、その手順から `agent-browser` の1手ずつの操作で引き継ぐ。セッションは結果の `session` にあり（`agent-browser --session <session> snapshot -i` → `click @eN`）、終わったら `agent-browser --session <session> close`。
- Cookie 同意などの重なりで `Element is covered by ...` になったときは、重なっているダイアログを閉じてから必ず `snapshot -i` を取り直す（再描画で `@eN` の番号が振り直される）。`href="javascript:void(0)"` のリンクは URL を直接開かずクリックで遷移させる。
- 同じ手順書を丸ごと再実行しない。手順はやり直しが効かず、選択済みの項目をもう一度選ぼうとして止まることがある。`agent-browser eval` で今の状態を確かめ、済んだ手順を除いた手順書または直接操作で続ける。
- 原稿どおりの文を入れる欄では、1回入力したらすぐに表示された値（リッチテキストのエディタでは表示された本文）を読み戻し、原稿と比べる。一致しなければ入力を繰り返さず、実際に編集できる要素を調べ直す。入力を繰り返すと、文が追記されて重複することがある。
- ログインを含む作業を `agent-browser` で直接行うときは、`agent-browser --session <名前> --profile "$(uv run --project "$ABD" agent-browser-decisions profile)" open <URL>` で起動する。ログイン成功後に Chrome のパスワード管理がページへの入力を止め、クリックや Enter が効かなくなるため（`reset` の手順書は自動で対処済み）。普段のブラウザのログイン状態（`--profile Default`）は、プロファイルがない、または OS の保護で読めずに使えないことがある。その前提で、最初から `--headed` を付けて開き、ログインは利用者に画面で行ってもらう。
- 後片付けでは、自分が開いた `--session` だけを閉じる。`close --all` は他の作業が使っているセッションも閉じるため使わない。
