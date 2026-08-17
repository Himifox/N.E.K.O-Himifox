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

アダプターは N.E.K.O が解決した conversation model 設定をメモリ上の Core route
へ投影します。API key を OpenBiliClaw の `config.toml` に複製しません。custom
および Qwen-compatible endpoint は OpenAI-compatible adapter を使用します。

content embedding は OpenBiliClaw 側で独立して設定します。N.E.K.O の character
memory vector とは schema と意味が異なるため、同じ store を共有しません。

## 状態と復旧

Main Server の `GET /api/openbiliclaw/status` は loopback-only です。状態、拡張
endpoint、data directory、degraded flag、secret を含まない error を返します。

- `NEKO_OPENBILICLAW_ENABLED=0` はこの組み込み統合だけを無効化します。
- `NEKO_OPENBILICLAW_PORT=<port>` は既定の `8420` を変更します。拡張機能側の
  endpoint も同じ値にする必要があります。

Core dependency は `pyproject.toml` と `uv.lock` で正確な commit に固定されます。
共有 `bilibili_api` import は N.E.K.O の `bilibili-api-dev` が提供し、競合する
upstream wheel は uv override で無効化します。
