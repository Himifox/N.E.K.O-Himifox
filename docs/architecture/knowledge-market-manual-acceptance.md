# 通用知识库市场三端手工验收

本文用于验证 Auth 平台、插件市场和 N.E.K.O 桌面端之间的知识包订阅链路。所有命令仅适用于本地开发环境，不得把文中的开发凭据用于公网部署。

## 1. 验收范围

本轮覆盖：

- Market Web 与 N.E.K.O Desktop 分别通过统一 Auth 登录同一账号；
- N.E.K.O 使用现有 `neko-desktop`、PKCE 和 loopback callback 完成授权；
- Market 页面通过短期一次性码获得当前本机的操作授权；
- 知识包下载、SHA-256 校验、身份校验、导入和订阅记录；
- 知识包管理、自动对话递卡和重启持久化；
- Auth、Market、N.E.K.O 任一端异常时的可诊断行为。

Auth 与本机授权职责不同：

```text
Auth OAuth：确认用户身份
一次性本机授权：确认网页可以操作当前这台 N.E.K.O
```

完整链路：

```text
Market Web OAuth ───────────────┐
                               ├─ 同一 Auth 用户
N.E.K.O Desktop OAuth ─────────┘
          │
          ├─ 一次性本机授权
          ├─ 下载并校验知识包
          ├─ 写入本地通用知识库
          └─ 使用 Desktop OAuth token 向 Market 报告订阅
```

## 2. 前置条件

- Windows 已安装并启动 Docker Desktop；
- Git Bash 或启用 Docker 集成的 WSL 可用；
- Python 命令统一使用项目的 `uv` 和 Python 3.11；
- 下列仓库已经存在：
  - `C:\Users\NEKO-PC-03\Documents\GitHub\neko-auth-platform`
  - `C:\tmp\neko-plugin-market-integration`
  - 当前 N.E.K.O 工作树；
- Market 数据库已经包含 `manual-test` 知识包；
- 测试 Release 中的知识包附件可正常下载；
- 启动前彻底退出旧 N.E.K.O，释放 `48911` 端口。

推荐启动顺序：

```text
Auth → Market Backend → Market Web → N.E.K.O
```

## 3. 启动 Auth

在 Git Bash 或 WSL 中执行：

```bash
cd /c/Users/NEKO-PC-03/Documents/GitHub/neko-auth-platform

export AUTHORIZATION_INTERNAL_TOKEN='local-dev-authorization-token-000000'
export AUTH_DOMAIN_AUTH_WEB_TOKEN='local-dev-auth-web-token-0000000000'
export AUTH_DOMAIN_MARKET_TOKEN='local-dev-market-token-000000000000'
export AUTH_DOMAIN_ORY_BRIDGE_TOKEN='local-dev-ory-token-000000000000000'

./scripts/dev-auth-up.sh
docker compose up -d mailpit
```

检查：

| 服务 | 地址 | 预期 |
| --- | --- | --- |
| Auth Web | `http://127.0.0.1:4455/auth?mode=login` | 显示登录页 |
| Hydra | `http://127.0.0.1:4444/.well-known/openid-configuration` | 返回 OIDC 配置 |
| Auth Domain | `http://127.0.0.1:4470/health` | 返回健康状态 |
| Mailpit | `http://127.0.0.1:8025` | 显示本地测试邮箱 |

`dev-auth-up.sh` 报告端口占用时，应先停止旧 Auth 容器或 SSH 隧道，不要改用随机端口绕过。

## 4. 启动 Market Backend

在新的 PowerShell 中执行：

```powershell
cd C:\tmp\neko-plugin-market-integration

$env:ENVIRONMENT = "development"
$env:DATABASE_URL = "sqlite+aiosqlite:///./plugin_market.db"

$env:ORY_HYDRA_ADMIN_URL = "http://127.0.0.1:4445"
$env:ORY_HYDRA_PUBLIC_URL = "http://127.0.0.1:4444"
$env:ORY_ISSUER = "http://127.0.0.1:4444/"
$env:AUTH_DOMAIN_SERVICE_URL = "http://127.0.0.1:4470"

$env:AUTHORIZATION_INTERNAL_TOKEN = "local-dev-authorization-token-000000"
$env:AUTH_DOMAIN_MARKET_TOKEN = "local-dev-market-token-000000000000"
$env:ACCOUNT_SUMMARY_INTERNAL_TOKEN = "local-dev-account-summary-token-0"

uv run --python 3.11 uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

检查：

- `http://127.0.0.1:8000/health` 返回健康状态；
- `http://127.0.0.1:8000/api/v1/knowledge-packages` 返回知识包目录；
- 目录中存在 `manual-test`；
- 后端启动日志没有 Auth Domain 凭据不匹配错误。

## 5. 启动 Market Web

在新的 PowerShell 中执行：

```powershell
cd C:\tmp\neko-plugin-market-integration\NEKO_Plugins_Market

$env:VITE_API_BASE_URL = "http://127.0.0.1:8000/api/v1"
$env:VITE_ORY_HYDRA_PUBLIC_URL = "http://127.0.0.1:4444"
$env:VITE_ORY_AUTH_WEB_URL = "http://127.0.0.1:4455"
$env:VITE_OAUTH_CLIENT_ID = "neko-plugin-market-web-dev"
$env:VITE_OAUTH_SCOPES = "openid email profile offline"
$env:VITE_OAUTH_REDIRECT_URI = "http://127.0.0.1:5173/oauth/callback"

npm run dev -- --host 127.0.0.1
```

检查：

- `http://127.0.0.1:5173/#/knowledge` 显示知识包目录；
- `http://127.0.0.1:5173/#/knowledge/manual-test` 显示测试包详情；
- 浏览器控制台没有 OAuth 初始化或 API 跨域错误。

## 6. 启动 N.E.K.O

必须在设置环境变量的同一个 PowerShell 中启动：

```powershell
cd C:\Users\NEKO-PC-03\.codex\worktrees\5ca3\N.E.K.O-Himifox

$env:NEKO_AUTH_URL = "http://127.0.0.1:4444"
$env:NEKO_MARKET_API_URL = "http://127.0.0.1:8000"
$env:NEKO_MARKET_WEB_URL = "http://127.0.0.1:5173"

uv run --python 3.11 python launcher.py
```

若知识页面仍打开 `https://market.project-neko.cn`，说明旧进程没有退出，或环境变量没有设置在启动 N.E.K.O 的同一个终端。

## 7. 手工验收用例

### 7.1 N.E.K.O Desktop OAuth

步骤：

1. 打开插件管理器的“知识库”页面；
2. 确认按钮显示“登录”；
3. 点击登录；
4. 确认浏览器打开本地 Auth 页面；
5. 登录或注册测试账号；
6. 新账号需要验证时，在 Mailpit 中完成验证；
7. 接受 `neko-desktop` 授权；
8. 确认回调进入 `http://127.0.0.1:48911/market/oauth/callback`；
9. 返回知识页面。

预期：

- 页面显示已连接的账号；
- N.E.K.O 不接触用户密码；
- access token 和 refresh token 只由本地 OAuth 运行时持有；
- state、PKCE 和回调校验失败时不会建立登录状态。

### 7.2 Market Web OAuth

步骤：

1. 在 Market Web 点击登录；
2. 使用与 N.E.K.O 相同的 Auth 账号；
3. 完成 `neko-plugin-market-web-dev` 授权；
4. 等待回调返回 Market Web。

预期：

- Market Web 显示登录状态；
- Market Web 与 Desktop 使用不同 OAuth client，但映射到同一 Auth 用户；
- Market Web token 不复制给 N.E.K.O，Desktop token 也不暴露给网页。

### 7.3 本机操作授权

步骤：

1. 回到 N.E.K.O 知识页面；
2. 点击“打开知识市场”；
3. 不要继续使用授权前手工打开的旧 Market 标签页。

预期：

- Market URL 短暂携带一次性授权参数；
- 参数被前端立即从地址栏清除；
- 页面不再提示“请先启动并配对 N.E.K.O”；
- 同一个一次性码不能兑换两次；
- 非本地来源不能直接要求 N.E.K.O 生成一次性码。

### 7.4 订阅知识包

打开：

```text
http://127.0.0.1:5173/#/knowledge/manual-test
```

点击“订阅到 N.E.K.O”。

预期阶段：

```text
连接本地 N.E.K.O
→ 下载知识包
→ 校验 SHA-256
→ 校验 schema、collection_id、pack_id
→ 写入本地通用知识库
→ 向 Market 报告订阅
→ 完成
```

随后在 N.E.K.O 知识管理页检查：

- `manual-test` 已出现在知识包列表；
- 版本为 `1.0.0`；
- 条目数为 5；
- 订阅来源为 `plugin-market`；
- 可以单独启用或关闭自动对话参与；
- 重复订阅不会重复导入词条。

### 7.5 查询与管理

在知识管理页面完成：

1. 按标题查询测试包词条；
2. 打开词条详情；
3. 检查 `title / terms / tags / summary / content`；
4. 禁用并恢复一条词条；
5. 关闭并重新打开知识包的自动对话参与；
6. 检查最近命中诊断。

预期：

- Market 元数据不会污染五字段知识词条；
- 禁用只影响对应条目；
- 自动参与按知识包隔离；
- 管理操作不会写入用户记忆文件。

### 7.6 猫娘递卡

启用 `manual-test` 自动对话参与后，依次发送：

```text
今晚月亮不太对劲，做决定还是谨慎一点。
今天由纸箱船长指挥。
情况不稳定，先启动蓝莓协议。
风越大信送得越快，今天也得把任务交付。
今天的电量是周三限定，下午可能就没劲了。
```

预期：

- 对应词面在本地命中；
- 每轮最多递送一张临时知识卡；
- 猫娘先理解语境，再自然回应；
- 不主动提及知识库、检索或来源；
- 临时卡不进入聊天历史、用户记忆或人格文件；
- 未命中内容保持普通对话路径。

### 7.7 重启持久化

步骤：

1. 完成订阅后退出 N.E.K.O；
2. 保持 Auth 与 Market 运行；
3. 使用第 6 节相同环境变量重新启动 N.E.K.O；
4. 再次检查知识包并重复一条递卡语句；
5. 从 N.E.K.O 再次打开 Market。

预期：

- `manual-test` 和五条知识仍存在；
- 不产生重复词条；
- 自动参与状态保持；
- 本地 bridge token 随 N.E.K.O 重启失效；
- 新 Market 页面可通过新的一次性码重新建立本机操作授权；
- Desktop OAuth 可通过 refresh token 延续，不要求每次启动重新登录。

## 8. 故障边界

| 现象 | 优先检查 |
| --- | --- |
| 打开生产 Market | N.E.K.O 启动终端中的 `NEKO_MARKET_WEB_URL` 和旧进程 |
| Auth 提示 redirect URI 非法 | 重新执行 `dev-auth-up.sh` 注册本地 Hydra clients |
| Market 提示未配对 | 从 N.E.K.O 知识页面重新打开 Market，不复用旧标签页 |
| Market API 返回 401 | 对应 Web/Desktop OAuth 是否完成，token 是否属于正确 client |
| Auth Domain 返回 401/403 | Auth 和 Market 的开发内部凭据是否完全一致 |
| 知识包下载 404 | Release tag、附件名和 `artifact_url` |
| SHA-256 不一致 | Market 版本摘要与 Release 附件是否对应 |
| 订阅完成但不递卡 | 知识包自动参与开关、词面是否命中、词条是否禁用 |
| N.E.K.O 重启后 Market 失去连接 | 预期行为；从知识页面重新打开以生成新一次性码 |

## 9. 通过标准

同时满足以下条件才视为三端验收通过：

- Market Web 与 N.E.K.O Desktop 均通过本地 Auth 成功登录；
- 两个 OAuth client 映射到同一用户且 token 不跨端复制；
- Market 页面只能通过短期一次性码操作当前 N.E.K.O；
- `manual-test` 可完成下载、校验、导入和订阅报告；
- 本地管理、禁用、自动参与和诊断有效；
- 猫娘能消费订阅知识包产生的临时卡；
- N.E.K.O 重启后知识包持久存在，授权边界符合预期；
- 日志中没有 bridge token、OAuth token、用户原话或知识正文泄漏。

## 10. 停止环境

- Market Backend、Market Web 和 N.E.K.O：在各自终端按 `Ctrl+C`；
- Auth：

```bash
cd /c/Users/NEKO-PC-03/Documents/GitHub/neko-auth-platform
docker compose down
```

`docker compose down` 默认保留持久化数据卷；只有明确需要清空本地 Auth 测试数据时才另行删除卷。
