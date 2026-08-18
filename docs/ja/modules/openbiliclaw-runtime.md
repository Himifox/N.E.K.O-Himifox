# OpenBiliClaw 組み込みランタイム

> **現在の契約。** N.E.K.O Main Server が直接所有するファーストパーティ統合です。

`app/openbiliclaw_runtime.py` は 1 個の `OpenBiliClawCore` をプロセス内に組み込み、
既存のブラウザー拡張 API を `http://127.0.0.1:8420` で提供します。ユーザー
プラグインでも MCP チャンネルでもないため、それらのスイッチには依存しません。

## ライフサイクルと保存先

- N.E.K.O のストレージ初期化後に Main Server が Core を起動します。
- 終了時は loopback Uvicorn bridge を停止し、ASGI shutdown が Core の task、
  queue、client、database を閉じます。
- データは `<N.E.K.O data root>/integrations/openbiliclaw/` に分離され、character
  memory database とは共有しません。
- import または port bind に失敗すると状態は `unavailable` になりますが、
  N.E.K.O 自体の起動は継続します。

拡張機能は従来の `/api/*` HTTP/WebSocket 契約を使い、別途
`openbiliclaw start` を実行する必要はありません。N.E.K.O 停止中は listener も
停止しますが、offline buffer 対応版の拡張機能は再起動後にイベントを再送できます。

## モデル境界

`NekoManagedLLMProvider` は OpenBiliClaw の各 background call で N.E.K.O の現在の
conversation model snapshot を読み、既存の `create_chat_llm_async()` 経路で実行します。
model／route の変更は Core の再起動なしで次回 call から有効です。API key は
N.E.K.O の解決済み設定と call 中の memory にだけ存在し、OpenBiliClaw の
`config.toml` には書きません。call は `openbiliclaw` usage category に記録され、
output budget、JSON、timeout、cancel、usage mapping を扱います。temperature は
N.E.K.O の既存方針どおり強制しません。

起動時に adapter は embedded config の standalone LLM instance を credential を
含まない `neko-conversation` placeholder へ移行します。同じ projection を初回構築と
Core の各 hot reload 前に再適用するため、source initialization や settings save が
古い DeepSeek/OpenAI direct route を再有効化することはありません。conversation route
を一時的に解決できない場合も direct access へ fallback せず fail closed します。
組み込みの `free-model` service は user chat 専用で background profile/candidate
analysis を拒否します。adapter はこの route を無効化し、Core を degraded bridge
mode に保って反復 request を止めます。analysis を有効にするには background use を
許可する conversation model を N.E.K.O に設定し、N.E.K.O を再起動してください。

「model の統一」は route、credential、最終 speaker の所有権を統一する意味で、
system 全体の model request が 1 回だけという意味ではありません。OpenBiliClaw は
profile analysis、candidate evaluation、recommendation copy の background work に
同じ managed route を引き続き利用できます。

content embedding は OpenBiliClaw 側で独立して設定します。N.E.K.O の character
memory vector とは schema と意味が異なるため、同じ store を共有しません。

## 単一 speaker と recommendation handoff

```text
OpenBiliClaw background → N.E.K.O-managed model route → structured pool
N.E.K.O proactive chat → preview（LLM なし・非消費）→ 既存 Phase 1
                      → 既存 Phase 2（唯一の表示台詞）
                      → delivery 成功 → shown を確認
```

- healthy Core から 1 round 最大 3 件を preview します。preview は source refresh、
  LLM call、表示履歴 write を行いません。
- 既存 Phase 1 の total budget に OpenBiliClaw 用 1 slot を予約し、他 source は
  round-robin を継続します。二つ目の Phase 1 は追加しません。
- 最終台詞は N.E.K.O の persona、memory、language を使う既存 Phase 2 だけが生成します。
  normal／proactive chat と tool chain は `core.chat()` を呼びません。
- `[PASS]`、user takeover、delivery failure、degraded Core、empty pool、timeout では
  candidate を消費せず、他の proactive source も止めません。
- prompt には bounded candidate fields だけを渡し、OpenBiliClaw の full profile は
  注入しません。

browser extension は引き続き platform behavior と browser session を収集する「手足」です。
N.E.K.O plugin system と MCP は不要ですが、extension 自体の install／設定は必要です。
N.E.K.O 停止中は event を保持し、復旧後 `127.0.0.1:8420` へ再送します。

## 状態と復旧

Main Server の `GET /api/openbiliclaw/status` は loopback-only です。状態、拡張
endpoint、data directory、degraded flag、secret を含まない error を返します。

- `NEKO_OPENBILICLAW_ENABLED=0` はこの組み込み統合だけを無効化します。
- `NEKO_OPENBILICLAW_PORT=<port>` は既定の `8420` を変更します。拡張機能側の
  endpoint も同じ値にする必要があります。

Core dependency は `pyproject.toml` と `uv.lock` で正確な commit に固定されます。
共有 `bilibili_api` import は N.E.K.O の `bilibili-api-dev` が提供し、競合する
upstream wheel は uv override で無効化します。
