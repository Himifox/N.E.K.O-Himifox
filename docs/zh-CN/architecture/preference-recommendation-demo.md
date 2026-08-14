# 基于现有 Web/Music 链路的普通聊天推荐 Demo

## 1. 验证目标与结论边界

本 Demo 只验证以下因果链：

```text
现有 Web 候选
  -> 本地确定性主题分类
  -> Web 候选池临时加权
  -> 现有 Phase 1 按标题选择
  -> Web 链接成功提交
  -> 按角色登记进程内回执
  -> 后续资源 Phase 1 从普通用户回复提取态度
  -> 再下一次资源 Phase 1 的 Web 候选池发生变化
```

它不代表生产推荐系统已经完成。Web 反馈只改变 Web 候选主题概率；明确 Music 兴趣只改变后续泛化 Music 请求的搜索关键词。Meme 仍只在 Phase 1 前参与搜索任务名额分配，实际资源继续在 Phase 1 后按原链路抓取并使用原素材衰减。

明确不包含：统一媒体排序、crawler 新主题字段、候选 ID 契约或硬校验、长期画像、持久化偏好、前端按钮、行为埋点、数据库、新 LLM 调用，以及新的媒体重复时间。

## 2. Web 候选与资源身份

分类和选择只读取现有 Web 字段：

```json
{
  "title": "Python桌宠开发记录",
  "url": "https://example.com/item",
  "source": "Bilibili",
  "mode": "video"
}
```

分类输入限定为 `title`、`description_hint`、`reason`、`source`、`url`。资源身份继续调用 `_source_hash(url, title)`：有 URL 时使用现有规范化 URL 哈希，没有 URL 时使用现有标题哈希降级。没有新增 `candidate_id`、`source_id` 或持久化字段。

## 3. 确定性一级主题

候选最多得到一个主主题：

```text
technology, programming, digital_devices, science, games,
anime_comics, music_culture, film_tv, internet_culture,
books_education, art_creative, finance_business, society, sports,
automotive, health_fitness, food_culture, travel_culture,
fashion_lifestyle, pets_animals
```

固定关键词表按命中数选择；同分按上述顺序决定；零命中为空主题。分类不调用 LLM，`news/video/music/meme` 不是主题，内部修正键仅使用 `topic.*`。

## 4. 回执与反馈验证

回执只在主动搭话成功提交后登记，并且必须同时满足：

- Phase 2 实际选择 `[WEB]`；
- 存在 `selected_web_link`；
- 现有 `_is_link_selected()` 确认该链接位于实际发送的 `source_links`；
- `_source_hash()` 返回非空资源键。

标题没有匹配到真实链接、纯聊天、Music、Meme 都不登记。回执按角色保存在进程内，每个角色最多 10 条，2 小时后过期，不写入跨角色 source history。

```json
{
  "receipt_id": "derived-from-turn-and-resource",
  "turn_id": "existing-proactive-sid",
  "resource_key": "existing-source-hash",
  "source_key": "normalized-host-or-source",
  "title": "实际发送标题",
  "primary_topic": "programming",
  "delivered_at": 1234567890,
  "evidence_snapshot": {}
}
```

`evidence_snapshot` 保存投递时已有用户行的指纹与次数。反馈只接受快照之后新增的用户原话；因此相同文字在投递前已经出现时，必须在投递后再次真实出现才可作为证据。

下一次确实包含资源任务的现有 Phase 1 调用会按需附加：

```text
[RECOMMENDATION_FEEDBACK]
{"receipt_id":"rec-001","reaction":"not_interested","confidence":0.91,"evidence":"这种游戏我没兴趣"}
```

固定 reaction 为 `positive`、`not_interested`、`quality_issue`、`source_distrust`、`temporary_skip`、`unclear`。解析器继续只返回同一个反馈对象；本地处理器根据 `receipt_id` 或固定的 `preference_type` 决定状态类型。功能开启时反馈任务随现有资源 Phase 1 注入，即使没有 Web 回执也可提取明确 Music 兴趣；不会产生新调用或线程。原始反馈段和 evidence 在既有 Phase 1 日志中统一脱敏。

## 5. 本地短期规则

反馈必须满足：回执未超过 2 小时、`confidence >= 0.6`、evidence 能在快照后新增用户原话中定位、同一用户原话/回执/reaction 未处理过。

- `positive`、`not_interested`：分别记录主主题正、负证据。证据只保留 2 小时；两个不同 `resource_key` 对同一主题给出同方向证据后，才形成 5 小时 `topic.*` 修正。分值为两条置信度均值乘方向，修正按主题覆盖而非累加。
- `quality_issue`：不改主题和来源；资源重复仍由成功投递后的现有 source history 处理。
- `source_distrust`：仅为当前角色抑制相同 `source_key` 的 Web 候选 5 小时；过滤在候选分组和探索之前。
- `temporary_skip`：不形成主题或来源状态；具体资源仍受现有 5 小时硬去重保护。
- `unclear`：不形成任何状态。

主题分数只作用于 Web 候选。空主题候选保留现有来源权重和顺序；负修正通过有限指数权重降低概率，不会把主题概率变成零。15% 探索保留，但候选先经过 `_should_skip_source()` 和角色来源抑制，所以探索不能绕过硬约束。Music/Meme 搜索任务是中性候选，不读取 `topic.*`。

## 5.1 明确 Music 兴趣

Music 兴趣与 Web 反馈复用同一个可选输出段，不新增解析标签：

```text
[RECOMMENDATION_FEEDBACK]
{"preference_type":"music_intent","value":"jazz","reaction":"positive","confidence":0.92,"evidence":"最近挺喜欢爵士"}
```

第一版只接受 `music_intent` 与 `music_artist`。音乐意图固定为 `pop`、`rock`、`electronic`、`hip_hop`、`jazz`、`classical`、`lofi`、`soundtrack`；歌手值必须逐字出现在最新用户原话中。`reaction` 只接受 `positive` 与 `not_interested`，`confidence` 必须不低于 0.6。“这首”“这种”“这个歌手”等无法可靠确定目标的表达不形成状态。

Music 兴趣按角色在进程内最多保存 6 项，有效 5 小时，相同 `preference_type + value` 的新表达覆盖旧表达。正向兴趣只在 Phase 1 输出为空或 `personalized` 时替换后续搜索关键词；点歌、歌单、`source:liked`、`source:daily` 和当前明确关键词保持原样。负向兴趣只取消同目标的正向兜底，不在抓取后过滤歌曲。`topic.music_culture` 仍表示 Web 音乐文化内容，不与实际听歌兴趣混用。

实现会在资源 Phase 1 前冻结 Music 兴趣快照，在同一次调用返回后再处理新的明确兴趣。因此新状态不会反写本轮后置搜歌，只能影响再下一次泛化 Music 任务。当前实现把兴趣值转换成公开搜索词，不改变网易云账号个性化候选的内部排序。

## 6. 生效时序

真实时序不能简写成“下一轮立即生效”：

```text
第 N 轮：Web 资源实际发送，登记回执
用户回复：在普通聊天中表达态度
第 N+1 次资源 Phase 1：候选已按旧状态选完；同一次调用提取反馈
第 N+2 次资源 Phase 1：才读取更新后的临时主题状态并改变 Web 候选池
```

如果 2 小时内没有再次执行包含资源任务的 Phase 1，回执过期，之后不再提取该反馈。只有一条主题证据时也不会改变概率。

## 7. 功能开关与手动验证

功能默认关闭；修改环境变量后需重启：

```powershell
$env:NEKO_PROACTIVE_PREFERENCE_DEMO_ENABLED='1'
```

离线固定运行：

```powershell
& '.venv\Scripts\python.exe' -m main_logic.proactive_chat.preference_recommendation
```

应看到：`new_llm_calls` 和 `new_llm_threads` 都为 0；第一条游戏负向证据后没有主题分数；第二个不同游戏资源产生第二条负向证据后形成 `topic.games` 负修正；再下一次候选池的游戏概率下降；`music_meme_behavior_changed` 为 `false`。

真实链路手测：

1. 开启功能并重启，准备至少两个来源或 URL 不同的游戏 Web 候选，以及非游戏对照候选。
2. 触发主动搭话，确认 `[WEB]` 链接 A 实际出现在响应 `source_links`，日志出现脱敏后的回执登记信息。
3. 用户普通回复“这种游戏我没兴趣”。
4. 触发下一次包含资源任务的主动搭话；确认该轮候选仍按旧状态选择，Phase 1 同一次调用接受第一条反馈，但没有 `topic.games` 修正。
5. 让不同 `resource_key` 的游戏链接 B 实际发送，再回复“还是不想看游戏”。
6. 再触发一次资源 Phase 1；确认该轮先按旧状态选候选，再提取第二条证据并形成 5 小时负修正。
7. 再触发一次资源 Phase 1；确认游戏进入 Top K 的概率和候选占比下降，同时现有硬去重、来源抑制和探索约束仍成立。
8. 用户明确回复“最近挺喜欢爵士”，触发下一次资源 Phase 1 提取 Music 兴趣；确认该轮仍使用冻结前的旧状态，再下一次泛化 Music 任务才把 `personalized` 改为 `jazz` 搜索。点歌、歌单、liked/daily 来源保持原行为。
9. 验证 Meme 仍按原后置抓取和素材衰减工作；Music 只改变泛化搜索词，不描述为曲风分类或账号候选重排。
10. 关闭功能并重启，确认提示词、解析后处理、回执、Web 加权和 Music 兴趣全部不启用。

## 8. 自动测试范围

自动测试覆盖 20 个主题、确定性与字段边界、空主题、真实 Web 投递回执门、2 小时过期与投递后证据、模型标签隔离、两资源证据门槛、5 小时修正、各非主题 reaction、角色来源隔离、现有 5 小时硬去重和概率衰减、探索硬约束、N+2 时序、零新增调用/线程、关闭开关、多语言提示词契约，以及 Python 3.11 环境。

## 9. 验收表述

> Demo 证明普通聊天反馈能够在不新增 LLM 调用的情况下，经过两次后续资源 Phase 1，改变 Web 候选池的主题概率和候选组成；明确音乐兴趣也能改变后续泛化 Music 请求的搜索关键词和实际候选组成。

不得扩写为歌曲已被准确分类、网易云账号候选已按本地兴趣重排、Meme 实际内容已个性化，也不得声称最终 LLM 文本受到候选 ID 硬约束。
