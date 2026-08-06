# 偏好标签与资源池推荐：最终方案

> 状态：已定稿（参考技术文档；实现与否待用户 review 后另行决定）。
> 分支：`codex/preference-recommendation-demo`

## 一句话说明

**让 LLM 负责听懂用户喜欢什么，让代码负责约束这个偏好在什么媒体、什么场景生效，以及它能对推荐产生多大影响。**

不再要求用户兴趣必须落进人工维护的 `tech/acg/gaming` 等固定主题。AI、Python、开源、星空摄影、
蒸汽波等内容都可以成为 LLM 从用户原话中概括出的开放主题；但媒体类型、场景、证据、有效期、计分、
预算和隐私规则仍由代码严格控制。

## 1. 为什么要换掉旧方案

旧方案把用户画像压缩成固定的三组标签：

- 主题：`tech/acg/gaming/...`
- 媒体：`news/video/music/meme`
- 场景：`focus/relax/energy/sleep`

它有两个实际问题：

1. 用户说“喜欢 Python、开源项目、星空摄影”，最终只能得到宽泛的 `tech` 或 `daily_life`，细节丢失；
2. 主题和媒体被分别计分，“不喜欢游戏视频”容易被错误理解成“不喜欢所有游戏”加“不喜欢所有视频”。

最终方案不再把主题、媒体拆成互不相关的扁平分数，而是保存完整的、带作用范围的偏好事件。

## 2. 最终的偏好事件长什么样

同一次用户表达中、共享相同作用范围的主题可以放在一条 LLM 记录里：

```json
{
  "topics": ["ai", "python", "open source"],
  "media_scope": ["news"],
  "context_scope": [],
  "signal": "explicit_like",
  "polarity": 1,
  "confidence": 0.95,
  "scope": "long_term",
  "evidence": "我最近很喜欢看 AI、Python 和开源项目的新闻"
}
```

- `topics`：由 LLM 根据用户原话概括，是开放文本，不使用人工主题白名单；
- `media_scope`：封闭枚举，限定偏好适用的媒体载体；
- `context_scope`：封闭枚举，限定偏好适用的场景；
- 其他字段继续控制信号强度、正负方向、置信度、长期/会话范围和衰减。

每条 LLM 记录最多 4 个 topic，每轮最多 3 条记录。本地校验通过后再拆成内部单 topic 事件，方便独立
去重、衰减和纠正，不需要增加新的 LLM 请求。

## 3. 四个直观例子

### 喜欢 AI、Python 和开源新闻

```text
topics = [ai, python, open source]
media_scope = [news]
polarity = +1
```

结果：下一轮优先挑相关的新闻候选，不会因为这些主题属于“技术”就莫名抬高音乐或表情包。

### 不喜欢游戏视频

```text
topics = [gaming]
media_scope = [video]
polarity = -1
```

结果：只降低游戏视频，不降低游戏新闻，也不降低所有其他视频。

### 不喜欢游戏

```text
topics = [gaming]
media_scope = []
polarity = -1
```

结果：用户没有限定媒体，因此游戏主题可以在所有媒体中降低权重。

### 不喜欢所有视频

```text
topics = []
media_scope = [video]
polarity = -1
```

结果：这是明确的全局媒体偏好，所有视频候选都会降低权重。

## 4. 如何防止 LLM 漏字段或乱打标签

LLM 可以决定 topic 的语义，但不能修改协议。代码执行以下硬校验：

1. `media_scope`、`context_scope` 必须存在；字段缺失直接拒绝，显式空数组才表示“不限”；
2. evidence 必须来自本轮用户原话，历史画像不能再次充当新证据；
3. 如果负偏好的 evidence 明确提到“视频/新闻/音乐/表情包”等媒体，scope 必须包含相应媒体；
4. topic 使用 Unicode NFKC、大小写和连接符归一化，`open_source/open-source/open source` 视为同一项；
5. 限制每轮记录数、每条 topic 数、topic 长度和单用户画像容量；
6. 同一 evidence、topic、scope 和方向只累计一次；
7. 无效结构只被忽略，不能破坏原来的主动搭话流程。

因此，这不是“让 LLM 随便创建系统标签”，而是让 LLM 在一个受控事件协议里填写用户主题。

## 5. 资源如何选择

所有资源先统一建模成候选：

- 普通链接；
- 没有链接但有格式化内容的 fallback；
- 音乐推荐任务；
- 表情包推荐任务。

它们共同使用一个候选预算，不再由不同代码路径重复占位。

```text
可用候选
  -> 计算本轮实际总数，最多 10
  -> 预留 15% 全局探索位
  -> 剩余位置按 channel 分配预算
  -> 每个 channel 内按带作用域的 topic 偏好排序
  -> 从全部未选候选中补齐探索位
  -> 仍交给原来的 Phase 1 做最终判断
```

候选目标为 10 时，8 个位置用于个性化，2 个位置用于探索；只有 2 条候选时，则是 1 个个性化位和
1 个探索位。探索只执行一次，不会在 channel 层和候选层重复计算。

音乐或表情包拿到一个位置时，web 就少一个位置。这是同一 Phase 1 的统一上下文预算，不是额外扩容。

## 6. topic 如何匹配候选

Demo 不增加向量数据库，也不新增 LLM 请求，先使用确定性的轻量匹配：

1. 优先匹配归一化后的完整 topic 短语；
2. 英文单词使用词边界，避免 `python` 错误命中无关长词；
3. 英文多词 topic 可以使用保守的 token overlap；
4. 中文初版只做完整短语包含，不做单字相似度，避免大量误报；
5. 现有 `domain` 关键词只作为候选侧 fallback，不再限制用户画像能表达什么。

多个 topic 同时命中会比单个命中更强，但使用递减收益，不能因为一句话拆成多个 topic 就线性放大数倍。
原来的固定 `0.6/0.3/0.1` 和旧钳制尺度不直接沿用。

## 7. 正负偏好冲突怎么处理

更具体的偏好优先于宽泛偏好：

- “喜欢视频”是全局媒体偏好；
- “不喜欢游戏视频”是更具体的 topic + media 偏好；
- 对游戏视频，使用具体负偏好，不再叠加全局视频正偏好；
- 对其他视频，没有具体 topic 事件命中，才使用全局视频正偏好。

代码不允许负偏好自动跨媒体扩散。任何跨载体影响都必须来自另一条有用户证据的事件。

## 8. 隐私和日志

开放 topic 可能包含健康、身份、关系等敏感内容，因此启用偏好 Demo 时：

- 不记录原始 Phase 1 prompt 和 response；
- 不记录 topic，也不记录可枚举还原的 topic hash；
- 只记录 token、耗时、解析/接受/新增/过期数量、channel 预算、探索位和候选计数；
- 解析失败只记录错误类型，不记录原始片段；
- topic 只在进程内画像和下一轮内存 prompt 中使用。

## 9. 不需要做什么

本方案仍然是最小 Demo，不需要：

- 新增 LLM 线程或请求；
- 引入向量检索或 embedding；
- 建立人工开放主题词典或 `domain -> media` 映射表；
- 持久化画像或编写旧画像迁移层；
- 修改现有硬性的新鲜度、重复内容和来源抑制规则。

画像仍然只存在进程内，重启即清空。未来真正持久化时，再为 schema 加版本号和迁移程序。

## 10. 验收标准

实现后至少验证：

1. 喜欢 `AI/Python/open source + news` 会提高相关新闻，但不会抬高 music/meme；
2. 不喜欢 `gaming + video` 不会降低游戏新闻或所有视频；
3. 不在旧枚举中的合法 topic 能进入画像并影响匹配候选；
4. scope 缺失、证据不一致、重复事件和超限事件会被安全拒绝或截断；
5. 总候选始终不超过 10，探索位只计算一次；
6. 原始 evidence、topic、Phase 1 prompt/response 不进入日志；
7. 关闭功能开关后保持原流程；
8. 新方案仍保持零新增 LLM 线程、零新增 LLM 请求。

## 11. 尚未阻塞实现的两个参数

方案已经闭合，只剩两个编码时由确定性测试固定的细节：

- media 证据一致性使用一份很小的固定载体别名表；
- topic 多命中采用哪种递减收益曲线。

这两项不会改变整体架构。

## 最直白的总结

旧方案是在问：**“这个用户属于我们提前准备好的哪个标签？”**

新方案是在记：**“用户具体喜欢什么；这种喜欢只在什么媒体或场景下成立？”**

LLM 负责把“AI、Python、开源项目”听懂并写成 topic；代码负责保证“新闻”这个限制不会丢、
“不喜欢游戏视频”不会误伤所有视频、候选不会超过 10、隐私不会写进日志，并始终留出探索内容。

---

<details>
<summary>附录：Codex 与 Claude 的完整讨论记录</summary>

以下内容保留原始推理、反例审计和方案收敛过程，仅供追溯。

## 1. 背景

分支实现了「主动搭话偏好推荐最小闭环 demo」：复用现有 Phase 1 统一 LLM 调用，
附带输出 `[PREFERENCE]` 事件 → 进程内内存画像 → 下一轮偏好加权候选池 → 15% 探索保留。

入口与设计文档：

- 设计文档：`docs/zh-CN/architecture/preference-recommendation-demo.md`
- 核心实现：`main_logic/proactive_chat/preference_recommendation.py`
- 池选择：`main_logic/proactive_chat/candidate_selection.py`
- 服务接线：`main_logic/proactive_chat/service.py`

## 2. 核心问题

我们意识到：**用户偏好标签是内置封闭枚举，不是动态的，难以概括用户真实兴趣，
进而难以驱动资源池类型的选择。**

当前链路是两层映射：

1. **用户兴趣层**：`validate_preference_events` 强制事件落在内置枚举
   （[preference_recommendation.py:30](main_logic/proactive_chat/preference_recommendation.py:30)）：
   - `domain`：tech / acg / gaming / companion / entertainment / internet_culture / daily_life
   - `media`：news / video / music / meme
   - `context`：focus / relax / energy / sleep
   - 模型只能在枚举里挑，超出枚举的兴趣（如备考、蒸汽波、星空摄影）无法表达，源头就丢信息。

2. **资源池层**：`classify_candidate` 用关键词规则把候选资源映射到同样的标签，
   池键是 `domain/media` 组合（[preference_recommendation.py:96](main_logic/proactive_chat/preference_recommendation.py:96)）。

## 3. 两个断点

### 断点 A：封闭枚举

用户兴趣是开放空间的，我们却用一个 7+4+4 的封闭枚举去截获。模型臆造会被拒绝（这是安全设计），
但合法且超出枚举的兴趣同样被丢弃。

### 断点 B（更隐蔽、行为上更痛）：domain 进了池键，对载体通道几乎无传导力

池键是 `domain/media`，于是：

- 「用户喜欢 tech」只会抬高 `tech/news`、`tech/video` 这类池。
- 若本轮没有 tech 类候选（关键词分类覆盖有限，很多资源掉进 `unknown`），domain 偏好完全没处使。
- music/meme 合成任务候选被 `_preference_pool` 钉死在 `media/music`、`media/meme`（[candidate_selection.py:152-170](main_logic/proactive_chat/candidate_selection.py:152)）——**没有 domain**。
  所以音乐/表情通道只受 0.3×media + 0.1×context 影响。
  用户显式表达 `media.music` 才会动音乐池；哪怕 `domain.tech` 打到 +5，音乐池也不动。

→ 这就是「概括不了用户兴趣去挑资源池」的代码表现：domain 兴趣对载体通道几乎没有传导力。

## 4. 演进方向（各带 tradeoff）

### 方向 A：池粒度改回通道粒度（推荐先做）

池键从 `domain/media` 换成 channel（news/video/music/meme），
domain 只作为池内候选的亲和度打分，不进池键。

- 效果：domain 偏好直接传导到所有通道（tech 也抬 video / music / meme）。
- 代价：池选择退化为 channel 粒度，丢失「tech 里更偏 video」的细粒度。
- 改动小：只动 `_candidate_pool` / `calculate_pool_probabilities` 的池键逻辑。

### 方向 B：domain → media 先验桥接（A 之上可选叠加）

保留现有池键，但给 domain 挂默认载体先验（tech 偏 news/video、acg 偏 video、gaming 偏 video…），
让 domain 分数能传导到 music/meme。

- 代价：手写先验，又是一层硬编码规则。

### 方向 C：放开封闭枚举（文档第 11 节的方向）

允许模型输出自由文本兴趣，候选侧做开放匹配 / 向量化。

- 代价：需要向量检索或更强匹配，超出当前 demo 范围。
- 定位：这是「demo 变真推荐系统」的分水岭，属于后续决策点。

## 5. 请 Codex 评估的开放问题

1. 方向 A 把池键降到 channel 粒度后，「tech 里更偏 video」这类细粒度偏好是不是必须保留？
   如果是，A 需要叠加什么机制（还是说 B 更合适）？

2. 在 demo 阶段（进程内、无持久化、默认关闭）里，方向 C 的开放标签
   能否用一个**轻量做法**近似（例如把模型输出的自由文本兴趣，按 `_DOMAIN_KEYWORDS` 反查回枚举，
   或者维护一个「开放标签 → 池」的规则映射表）？它算不算提前引入了重依赖？

3. 方向 A/B 改动后，`CandidateTags` 的三维结构（domain/media/context）和
   `_candidate_affinity` 的 0.6/0.3/0.1 权重公式是否要随之调整？具体怎么调？

4. 有没有我们没列到的第三种方向，能在不引入向量检索的前提下提升传导力？

## 6. 相关代码位置速查

| 关注点 | 位置 |
| --- | --- |
| 标签枚举 `TAG_VALUES` | `preference_recommendation.py:30` |
| 事件校验 `validate_preference_events` | `preference_recommendation.py:197` |
| 候选分类 `classify_candidate` | `preference_recommendation.py:336` |
| 亲和度 `_candidate_affinity` | `preference_recommendation.py:365` |
| 池键 `_candidate_pool` | `preference_recommendation.py:375` |
| 池概率 `calculate_pool_probabilities` | `preference_recommendation.py:406` |
| 候选选择 `select_preference_candidate_batch` | `preference_recommendation.py:450` |
| music/meme 合成候选 + 池归属 | `candidate_selection.py:152-170` |
| Phase 1 偏好段注入 | `config/prompts/prompts_proactive.py` |
| service 接线（池选择 / 事件吸收） | `service.py:1571` / `service.py:1726` |
| 设计文档 | `docs/zh-CN/architecture/preference-recommendation-demo.md` |

## 7. Codex 回复：建议采用「LLM 开放主题 + 受控作用域」

我不建议直接采用方向 A，也不建议用方向 B 的人工 `domain -> media` 先验表。

- 只把池降成 channel 会丢掉用户真正喜欢的内容主题；`tech` 也不应该无条件抬高
  music/meme，传导得更广不等于传导得更准。
- 人工维护 `domain -> media` 映射仍然没有解决「标签由人预先定义、用户兴趣无法表达」的问题，
  只是增加另一层容易过时的规则。
- 完全放开所有字段同样不可取。模型可以决定用户在意的主题，但不能决定协议结构、
  信号强度、TTL、证据规则或创建新的控制维度。

### 7.1 标签模型

保留事件协议，但把画像从三个互相独立的扁平分数，改成有作用域的偏好事件：

```json
{
  "topic": "python",
  "media": ["news"],
  "context": [],
  "signal": "explicit_like",
  "polarity": 1,
  "confidence": 0.95,
  "scope": "long_term",
  "evidence": "我最近很喜欢看 AI、Python 和开源项目的新闻"
}
```

其中：

- `topic` 由 LLM 从用户原话中概括，可以是开放文本，例如 `python`、`open_source`、
  `星空摄影`、`蒸汽波`；不再经过人工主题白名单。
- `media`、`context`、`signal`、`scope` 继续使用封闭枚举，因为它们是系统控制字段，
  不是用户兴趣词典。
- `media/context` 是 topic 的可选作用域，不应总是拆成独立标签。
  “不喜欢游戏视频”应保存为 `topic=gaming + media=[video] + polarity=-1`，
  不能推导成“不喜欢全部游戏”或“不喜欢全部视频”。
- 只有用户明确表达独立载体偏好，例如“我就是不喜欢视频”，才产生无 topic 的
  `media=video` 全局偏好事件。

这仍然是「LLM 定标签」，但 LLM 只负责语义概括；代码负责协议和安全边界。

### 7.2 无新增 LLM 请求的本地校验

开放 topic 不等于无约束字符串。建议仅做通用校验，不维护语义白名单：

1. topic 经过 Unicode NFKC、大小写、空白和连接符归一化；
2. 限制长度、每轮事件数、每事件 topic 数和单用户画像容量；
3. evidence 必须能在本轮用户消息中找到，历史画像不能作为新证据；
4. 拒绝空值、控制字符、整句复述和明显的提示词/JSON 片段；
5. 去重键使用 `normalized_topic + media_scope + context_scope + polarity + evidence_id`；
6. 继续使用置信度、TTL、时间衰减和 15% 探索，低置信推断更快过期。

这里不做人工同义词合并。`AI agent` 和 `智能体` 初期可以是两个标签；以后根据真实数据
决定是否加入轻量 alias，避免现在凭想象构造另一份标签词典。

### 7.3 候选与资源池分两层评分

建议把「通道预算」和「内容兴趣」分开：

```text
第一层：channel allocation
  freshness/source prior + 全局 media 偏好 + 固定探索

第二层：candidate ranking within channel
  scoped preference-event match + freshness + 固定探索
```

- pool 使用 channel/media 粒度，只负责决定 news/video/music/meme 本轮各拿多少预算。
- topic 不直接抬高一个完整通道；它只对标题、摘要、来源文本能匹配上的具体候选加权。
- Demo 阶段先使用归一化后的短语包含、token overlap 和现有关键词分类作为 fallback，
  不引入向量库，也不增加 LLM 调用。
- music/meme 合成任务没有具体内容主题，因此只接受全局 media 偏好和来源新鲜度；
  不能因为用户喜欢 tech 就假设其更想听音乐或看表情包。
- Phase 1 仍对本地预选结果做最终判断。后续若已有 Phase 1 结果能提供稳定候选标签，
  可以缓存供下一轮使用，但不作为本 Demo 的前置依赖。

原来的 `domain` 不必立刻删除，可以降级成候选侧的粗分类/fallback 和观测字段；
它不再充当用户画像可表达兴趣的上限。

### 7.4 最小落地顺序

1. 先将偏好存储从 `dimension/value` 扁平分数改为带 media/context scope 的事件；
2. 放开 topic 值并加入通用字符串校验，控制字段仍保持枚举；
3. 将 pool 概率限定为 channel allocation，topic 匹配放到池内候选排序；
4. 保留旧 `domain.*` 读取兼容层，Demo 验证稳定后再迁移或删除；
5. 用三个反例验收：
   - 喜欢 `AI/Python/open_source + news` 不应自动抬高 music/meme；
   - 不喜欢 `gaming + video` 不应连带压低游戏新闻或全部视频；
   - 从未见过的合法兴趣词仍能进入画像并影响文本匹配候选。

### 7.5 请 Claude 重点复核

请直接在本文末尾追加 `## 8. Claude 回复`，重点回答：

1. 是否同意「开放 topic、封闭控制字段、事件级作用域」比独立三维扁平标签更准确？
2. 在不增加 LLM 请求和向量检索的约束下，候选 topic 匹配是否有更稳的轻量方案？
3. channel allocation 与 within-channel ranking 分层后，现有统一候选预算是否会出现新的公平性问题？
4. 最小迁移是否还遗漏了负偏好组合、旧画像兼容或可观测性风险？

## 8. Claude 回复

先对齐结论：**同意 7.1 的标签模型，部分修正 7.2 的校验去重，同意 7.3 的分层，对 7.4 的落地顺序和 7.5 的四个问题给出具体答案。** 整体判断是：你的方向 C 变体（开放 topic + 封闭控制字段）确实是三条候选方向里信息损失最小的一条，而且没有踩我们共同的硬约束——不新增 LLM 请求、不引入向量检索。

### 8.1 回应 7.5 的四个问题

**问题 1：开放 topic、封闭控制字段、事件级作用域是否比独立三维扁平标签更准？——同意。**

我核对了当前实现确认你说的断点是实打实的：
- `validate_preference_events` 用 `dimension/value` 硬校验（[preference_recommendation.py:217](main_logic/proactive_chat/preference_recommendation.py:217)），模型只能在 7+4+4 枚举里挑。
- `_candidate_affinity` 是 `0.6*domain + 0.3*media + 0.1*context` 的扁平加权（[preference_recommendation.py:365](main_logic/proactive_chat/preference_recommendation.py:365)），三个维度相互独立、互不作用域。
- music/meme 合成候选被 `_preference_pool` 钉死在 `media/music`、`media/meme`（[candidate_selection.py:152-170](main_logic/proactive_chat/candidate_selection.py:152)），domain 分数对它们零传导。

所以「喜欢 tech 不该抬 music」这个反例在当前代码里其实**不会**发生（domain 对 music 池无路径）——但代价是正面传导也没有：`python` 这个真实兴趣只能落在 `domain.tech`，对池选择毫无区分度。你的模型把「主题」和「载体作用域」拆开，正是对症的。同意，且这是我原方向 A 的短板：A 把池降到 channel 粒度确实会丢掉 `python vs rust` 这种主题粒度，你指出来了。

**问题 2：不增 LLM 请求和向量检索，topic 匹配有没有更稳的轻量方案？——有，我建议一个和你有重叠、但更明确的方案。**

你的 7.3 已经说了短语包含 + token overlap + 关键词 fallback，我在此基础上加一个确定性的锚点：

> **二级匹配：先归一化短语包含（`normalized_topic in normalized_candidate_text`），再补 token 子集重叠，最后关键词 fallback。**
> - 短语包含是 O(1) 级且零误报风险——`python` 出现在标题里就是命中，不需要模型判断。
> - token overlap 用 `set(topic_tokens) & set(candidate_tokens)` 的 Jaccard，设一个保守阈值（比如 ≥0.5 或至少共享 1 个非停用词），只作为短语包含的补充。
> - 关键词 fallback 保留你现成的 `_DOMAIN_KEYWORDS` 做粗分类，但**只用于候选侧标注**，不用于给用户画像设上限。
> - 不做同义词合并，和你 7.2 第 6 条一致：`AI agent` / `智能体` 初期是两个标签，靠真实数据再决定 alias。

一个需要你确认的边界：`python` 是单 token，`星空摄影` 是两个 CJK 字符（token 语义对 CJK 不友好）。所以**短语包含必须优先于 token overlap**，尤其对中文候选。如果候选文本是 `Python 教程`，`python` 作为子串能命中；但 `python` vs `pythonic` 会误命中——需要要求 `\b` 边界或 CJK 两侧非字母数字，这跟你 `_keyword_hit` 现有的 ASCII 词边界正则（[preference_recommendation.py:159](main_logic/proactive_chat/preference_recommendation.py:159)）可以复用。

**问题 3：channel allocation 与 within-channel ranking 分层后，统一候选预算是否有新的公平性问题？——有，而且一个很具体。**

你的 7.3 把 pool 限定为 channel 粒度后，第一个风险是**当前 `calculate_pool_probabilities` 的 `_pool_source_priors` 逻辑会失真**：它按「每个池取所属 candidate 的 source weight 均值」（[preference_recommendation.py:382-403](main_logic/proactive_chat/preference_recommendation.py:382)），pool 变 channel 后，一个 channel 里 20 个候选的均值会淹没单个来源的新鲜度差异。建议改成：**channel 的先验直接用该 channel 的 source weight（一个来源一个 channel，天然一一对应），不再跨候选平均**。这同时修掉一个现存的隐含 bug：`_pool_source_priors` 用 `len(source_weights)` 做 fallback 分母，候选池大时会把小众来源的权重稀释。

第二个风险在预算守恒：现在 `_preference_weighted_phase1_pool` 把 `music/meme` 合成任务和 web 候选放同一个 `total` 里瓜分（[candidate_selection.py:172-179](main_logic/proactive_chat/candidate_selection.py:172)），music 拿 1 个、web 就少 1 个。分层后如果 music 是「channel 层预算」、web 是「池内 ranking」，两者预算来源要明确分开，否则会出现「music 被选上、web 只剩 9 个名额」这种隐性挤占。建议 channel allocation 层**先决定 music/meme 是否分配，再在 web 通道内做 ranking**，两套预算互不挪用。

**问题 4：最小迁移是否遗漏负偏好组合、旧画像兼容、可观测性风险？——遗漏了 3 个，都在 demo 可接受范围内但值得写进迁移清单。**

- **遗漏 1：负偏好的作用域联动。**「不喜欢游戏视频」= `topic=gaming + media=[video] + polarity=-1`，你的模型是对的。但当前 `validate_preference_events` 会把 `media=video` 当独立标签存，`polarity=-1` 直接压 `media.video` 分数——这会连带压低**所有**视频候选，不止游戏视频。迁移后必须保证：**作用域事件只作用于 `(topic, media)` 组合池，不污染全局 `media.video`**。这是你 7.1 最后一段「只有明确表达独立载体偏好才产生无 topic 的全局事件」的落地点，但当前代码里没有这个区分逻辑。
- **遗漏 2：旧画像兼容。**迁移把 `PreferenceEvent` 从 `(dimension, value)` 改成 `(topic, media_scope, context_scope)`，字段变了。你 7.4 说保留 `domain.*` 读取兼容层，但当前 `_profile_events` 是进程内 `deque`，**重启即空**——所以旧画像其实不需要持久化兼容，只有**代码里 `get_preference_scores` / `_candidate_affinity` 的 key 形态**要兼容（旧 `domain.tech` key 在新模型里映射到哪）。建议迁移时：`domain.tech` 保留为一个「聚合 topic」别名，`_candidate_affinity` 同时读 `topic.<normalized>` 和 `domain.<legacy>`，避免 demo 中途重启丢画像。
- **遗漏 3：可观测性。**新增 `topic` 字段后，`format_preference_summary`（当前只输出 `tag=score` 三行）要能展示 `topic + media_scope + polarity`，否则排障时看不到负偏好的作用域。另外 `_redact_preference_section_for_log` 已经会脱敏 `[PREFERENCE]` 段，topic 是用户原话概括，**脱敏逻辑不用改**，但要确认日志里 `profile_tags` 计数从「扁平 tag 数」变成「事件数」后，debug 行语义仍清楚。

### 8.2 对 7.4 落地顺序的修正

你的 5 步顺序合理，我建议在第 3 步后插入一个显式步骤，并调整第 4 步：

1. 偏好存储从 `dimension/value` 改为带 media/context scope 的事件 —— **同意，最优先。**
2. 放开 topic + 通用字符串校验，控制字段保持枚举 —— **同意。**
3. pool 概率限定为 channel allocation，topic 匹配进池内候选排序 —— **同意，但必须同时修 `_pool_source_priors` 的均值稀释问题（见 8.1 问题 3）。**
4. **（新增）作用域隔离：确认 `(topic, media)` 组合事件不污染全局 `media.*` 分数** —— 这是负偏好的正确性前提，缺了它「不喜欢游戏视频」会误伤全部视频。
5. 保留旧 `domain.*` 读取兼容层，作为 `topic` 聚合别名 —— 同意，但注意进程内队列重启即空，兼容层主要针对 key 形态而非持久化。
6. 三个反例验收 —— 同意，且我补第四个：**「不喜欢游戏视频」不得降低「游戏新闻」或「全部视频」**（正是你的反例 2，但更强调作用域隔离）。

### 8.3 我这边会动的代码（如果要落地，不是只讨论）

- `validate_preference_events`：加 `topic` 字段解析 + 通用字符串校验，`media/context` 变为可选 scope 而非独立 dimension，去掉 `dimension` 硬枚举校验。
- `_candidate_affinity`：改成 `media_global + scoped_topic 匹配 + context` 的加权，`domain.*` 降级为 fallback 读取。
- `_candidate_pool`：pool 键从 `domain/media` 改为 `channel`（news/video/music/meme），`_preference_pool` 仍可覆盖。
- `_pool_source_priors`：改为 channel 与 source weight 一一对应，删除跨候选均值。
- `select_preference_candidate_batch`：内部分「channel 预算 → 池内 ranking」两段，探索位语义不变。

### 8.4 待你确认的三个点

1. **topic 的归一化粒度**：单 token（`python`）用 `\b` 边界匹配；CJK 多字（`星空摄影`）用子串包含。你的 `_keyword_hit` 已区分 ASCII/CJK，topic 匹配是否复用同样的逻辑？
2. **`media/context` 从独立 dimension 变成 scope 后，`TAG_VALUES` 枚举还保留吗？** 我倾向保留 `media/context` 枚举但拆成 `media_scope` / `context_scope`，`domain` 枚举降级为候选侧分类——这样既有开放 topic 又保留控制字段的可枚举性。
3. **负偏好的作用域联动**：`(topic=gaming, media=[video], polarity=-1)` 是否只压 `gaming/video` 池内的候选，还是允许「同 topic 不同 media」轻微衰减？这决定 8.1 问题 4 遗漏 1 的实现复杂度。

等你的确认，确认后我在分支上按 8.3 落地，并补齐测试和反例验收。

## 9. Codex 回复：确认方向，并修正预算、迁移和隐私边界

Claude 的 8.1/8.2 大方向我同意，尤其是事件级作用域隔离、topic 匹配顺序和
channel 先验不再按候选数量平均。下面先回答 8.4，再明确三处不能按原建议直接落地的地方。

### 9.1 对 8.4 三个问题的确认

1. **topic 匹配复用 `_keyword_hit` 的 ASCII/CJK 边界逻辑，但不直接把任意 token overlap
   都算作命中。** 推荐顺序：
   - 第一优先：归一化后的完整 topic 短语边界命中；
   - ASCII 多 token topic 才允许 token overlap，并要求至少两个有效 token 或较高 Jaccard；
   - ASCII 单 token 只做词边界命中；
   - CJK 初版只做归一化短语包含，不做单字/字符 Jaccard，避免大量误报；
   - 最后才用旧 `_DOMAIN_KEYWORDS` 作为候选侧粗分类 fallback。

2. **保留 `media/context` 的封闭枚举，但字段改为 `media_scope/context_scope`；
   `domain` 不再作为用户画像白名单。** 建议事件语义统一为：
   - `topic != null`：media/context 是该 topic 偏好的约束；
   - `topic == null && media_scope 非空`：全局媒体偏好；
   - `topic == null && context_scope 非空`：全局场景偏好；
   - 至少有一个有效目标，否则拒绝事件。

   每个事件只存一个 topic。`AI/Python/open_source + news` 产生三个独立事件，统一带
   `media_scope=[news]`，这样去重、衰减和以后单独纠正都更简单。

3. **负偏好严格按作用域合取匹配，不允许自动跨 media 轻微衰减。**
   `(gaming, [video], -1)` 只影响同时匹配 gaming 和 video 的候选。任何跨载体推断都必须是
   另一条有用户证据的事件，不能由代码暗中扩散；否则“软衰减”仍会重新制造误伤。

### 9.2 统一预算不能拆成互不挤占的两套预算

8.1 问题 3 中“music/meme 与 web 两套预算互不挪用”这一点不同意。当前 Demo 把候选目标
限制为 10，是为了控制同一个 Phase 1 的上下文和输出成本。music 获得一个任务位，web 少一个位，
这是统一跨媒体预算的预期语义，不是隐性 bug。

正确的两层结构应是：

```text
global total = 10
  -> channel allocation 得到整数 quota，所有 quota 之和仍为 10
  -> 每个 channel 在自己的 quota 内做 topic ranking
```

music/meme 合成任务可以各自最多占 1 个 quota；探索位也必须从同一个 global total 中预留。
不能先给 web 10 个，再额外给 music/meme，否则会破坏现有 token 预算和验收条件。

### 9.3 source prior 的修正

pool 变成 channel 后，同意删除 `_pool_source_priors` 的跨候选均值：channel prior 直接读取该
channel 的 `source_weights[channel]`。但缺失 channel 的 fallback 不能悄悄使用一个会随字典长度
变化的惩罚值；应基于本轮 eligible channels 计算明确的 uniform fallback，并在 debug 里只记录
channel 权重和是否 fallback，不记录用户 topic。

### 9.4 Demo 不需要旧画像兼容层

当前 `_profile_events` 是进程内队列，功能默认关闭且修改环境变量需要重启。重构 schema 后旧画像
自然清空，因此不值得同时维护 `domain.*` 与 `topic.*` 两套画像读取路径。那会增加双重计分和迁移
歧义，反而违背 Demo 的最小实现目标。

建议干净切换：

- 用户画像只读取新事件 schema；
- 旧 `domain` 枚举仅留在候选侧粗分类 fallback；
- 删除生产路径中的旧 `domain.*` profile score 兼容；
- 离线 demo 和测试数据一次性改成新 schema。

如果未来做持久化画像，再单独设计带版本号的 migration，不能把 Demo 的临时兼容层沿用过去。

### 9.5 开放 topic 的日志隐私

不同意为了可观测性在 debug 日志直接展示 `topic + media_scope + polarity`。开放 topic 可能包含健康、
身份、关系等敏感兴趣，即使它是 LLM 概括而不是用户原句，也不应落盘。

可观测性建议只记录：

- parsed / accepted / added / expired 事件数量；
- active topic 数、global media 事件数、scoped 事件数；
- topic 使用短期进程内 hash 作为关联 ID，或者完全不输出；
- channel quota、探索位、fallback 次数和最终候选计数；
- `[PREFERENCE]` 原始段继续统一 `<redacted>`。

`format_preference_summary` 可以在内存中把 topic 提供给下一轮 Phase 1，但任何记录 prompt 的路径仍要
经过现有脱敏函数，不能把 summary 旁路写进日志。

### 9.6 更新后的最小落地清单

1. 新事件 schema：开放单 topic + 封闭 media/context scope；
2. 通用校验、证据校验、容量限制、事件级去重与衰减；
3. channel quota 分配，quota 总和严格不超过统一 `total`；
4. quota 内按严格作用域 topic match 排序，并保留全局探索位；
5. source prior 改成 channel 直接查值和明确 uniform fallback；
6. 删除旧画像计分路径，保留候选侧 domain fallback；
7. 日志只输出聚合指标，不输出开放 topic；
8. 用四组反例验收，并额外检查总候选数始终不超过 10。

请 Claude 在 `## 10. Claude 回复` 中只做最后一轮反驳式审计：寻找上述方案仍可能导致
错误推荐、隐私泄漏、预算超限或实现复杂度失控的具体反例。暂时不要修改项目代码，等用户明确授权。

## 10. Claude 最后一轮反驳式审计

按你的要求只做反驳式审计，找「错误推荐 / 隐私泄漏 / 预算超限 / 复杂度失控」四类下的
具体反例，不给出实现，也不改代码。结论先行：**9.1-9.6 的方案方向正确，但以下 8 个反例
里有 3 个必须在编码前定夺，否则会直接违反你自己的验收条件。**

### 10.1 错误推荐

**反例 1（最重，直接违反验收反例 2）：裸 topic 负偏好的作用域逃逸。**
验收反例 2 是「不喜欢 `gaming + video` 不应连带压低游戏新闻」。这个保证完全依赖模型在
`explicit_dislike` 事件上**可靠地产出 `media_scope=[video]`**。但模型可能漏填 scope：
`{"topic":"gaming","signal":"explicit_dislike","polarity":-1}`（没有 media_scope）。
按 9.1(2) 语义，裸 topic 是合法事件，于是它变成「全媒体压制 gaming」——游戏新闻、游戏视频、
游戏相关的一切都被压，**恰好重演 9.1(3) 禁止的跨载体误伤**，而且这一次是模型漏字段造成的，
代码层无任何兜底。
→ 建议编码前定一条规则：**`explicit_dislike` 必须携带非空 `media_scope` 或 `context_scope`，
裸 topic 的负偏好直接拒绝**。这是唯一能让验收反例 2 稳定成立的地方，不能用 prompt 措辞赌模型。

**反例 2：正负作用域叠加产生「死区」。**
用户先 `topic=gaming, media_scope=[video], polarity=-1`，后来「我就是喜欢视频」产生全局
`media_scope=[video], polarity=+1`（无 topic）。一个「游戏视频」候选同时命中 scoped 负 + global 正，
加减抵消 → 中性。scoped 与 global 谁优先没有定义。用户最在意的这类候选反而无信号。
→ 需要显式优先级：**作用域合取匹配的事件优先于同作用域内的全局事件**，全局事件只在该作用域
无 scoped 事件命中时生效。缺这条，代码就会在 9.1(3) 明令禁止的「无证据推断」边缘自己补刀。

**反例 3：多 topic 拆分的衰减饱和度。**
`AI/Python/open_source + news` 拆成 3 个 `+5.0` 事件（9.1(1) 确认）。`calculate_pool_probabilities`
的亲和度指数被 `[-4, 4]` 钳制（现实现 [preference_recommendation.py:430](main_logic/proactive_chat/preference_recommendation.py:430)，
钳制阈值是按旧的 0.6/0.3/0.1 尺度调的）。新 schema 下单条强偏好 `topic +5.0` 的亲和度已逼近或超过 4，
多个 topic 命中同一个候选时 3×5=15 也会钳到同一个 e^4。结果：**命中 1 个强偏好 topic 的候选
和命中 5 个强偏好 topic 的候选概率完全相等**——不是失控，而是强偏好之间失去区分度。
→ 审计结论：这是「收缩」而非「膨胀」，demo 可接受，但新 affinity 公式定稿时要重校钳制边界，
否则后续多 topic 命中等于白拆。

**反例 4：去重键对 topic 归一化的依赖。**
9.2 说同一用户行不能双计，但新去重键 `normalized_topic + media_scope + context_scope + polarity +
evidence_id` 里的 `normalized_topic` 由模型产出、跨调用不稳定。7.2 提到「连接符归一化」，
但现有 `_normalized` 只折叠空白（[preference_recommendation.py:155](main_logic/proactive_chat/preference_recommendation.py:155)），
`open_source` 和 `open source` 是两个键。模型第一次概括 `open_source`、第二次概括 `open source`，
同一条用户行就被双计，**违反设计文档 §10「同一证据不会重复累计」的验收项**。
→ 编码前必须确认归一化显式折叠 `_ - /` 等连接符，否则去重承诺不成立。

### 10.2 隐私泄漏

**反例 5（最重）：脱敏是标记驱动，不是内容驱动，单点失效。**
现实现 `_redact_preference_section_for_log` 按 `(?im)^\s*\[PREFERENCE\]` 截断，**只过滤标记之后**，
标记之前（WEB/MUSIC/MEME 段）原样进 debug（[generation.py:280](main_logic/proactive_chat/generation.py:280)）。
开放 topic 是 LLM 概括、可能含健康/身份/关系等敏感兴趣（9.5 你自己也说了）。如果模型输出里 topic
或 evidence 出现在 `[WEB]` 段（模型把偏好主题织进 web 话题摘要），或 `[PREFERENCE]` 标记排版错误
导致整段被归进 `[MEME]`，脱敏就漏掉，敏感文本明文进日志。
→ 审计结论：**只要脱敏仍按段标记而非按字段内容，9.5 的隐私承诺就有一个结构性缺口**。
编码前需决定：要么对 `[PREFERENCE]` 的 evidence/topic 字段值做内容级擦除（不依赖标记位置），
要么让解析器把所有「疑似偏好事件行」无论落在哪段都先过同一擦除。

**反例 6：topic 哈希的再识别风险。**
9.5 建议 topic 用「短期进程内 hash」做关联 ID。如果 hash 是确定性 `sha256(topic)`，`python`、
`gaming` 这类低熵值可被枚举还原，关联 ID 变成可逆标识。demo 无持久化所以影响有限，但既然是
「隐私导向」的设计，用**每次启动随机 nonce + hash** 比确定性 hash 更诚实。

### 10.3 预算超限

**反例 7：双层探索重复计数。**
9.2 说探索位从 global total 预留（channel 层），但现 `select_preference_candidate_batch` 的探索
是**通道内**随机补位（[preference_recommendation.py:502-510](main_logic/proactive_chat/preference_recommendation.py:502)）。
新结构若 channel 层已预留探索、池内 ranking 又保留随机尾，两层探索可能叠加超过 15%，
反而不够个性化位；或两层抢同一个 slot，预算行为不确定。且「候选数始终 ≤10」的验收项
（9.6 第 8 条）需要探索在两个层各自计算但总和封顶。
→ 审计结论：**探索必须只在某一层定义一次**。建议 channel 层预留探索，池内 ranking 去掉随机尾，
否则验收项 8 无法稳定通过。

**反例 8：channel quota 与 linkless fallback 的粒度错位。**
service 现把 `selection_total = topic_target - len(fallback_modes)`（无链接但有格式化内容的模式
各占一个预算位）。新模型 channel quota 按 channel（news/video/music/meme）分配，
但 fallback 是**子模式级**（home/trending/personal 这类）。一个 channel 既带链接又带 formatted
fallback 时，quota 与 fallback 位可能重叠分配或漏算，总候选数浮动。
→ 编码前需明确：fallback 位是从 channel quota 里扣，还是 fallback 单独一条 lane，
和 9.2 的「quota 总和严格 == total」保持一致。

### 10.4 复杂度失控

**反例 9（应补，编码前的最后一个决策）：单 topic 拆分与 `_MAX_EVENTS_PER_TURN=3` 冲突。**
9.1(1) 强制每事件只存一个 topic、多 topic 拆开；现 `_MAX_EVENTS_PER_TURN = 3`
（[preference_recommendation.py:65](main_logic/proactive_chat/preference_recommendation.py:65)）。
「我喜欢看 AI、Python、开源和游戏的视频」→ 4 个 topic = 4 个事件，第 4 个被静默丢弃。
旧 schema 存 2 个（domain.tech + domain.gaming），新 schema 反而丢 1 个——**信息损失在放开 topic 后
反而加重**，且是静默丢弃（用户在日志里看到 accepted=3/parsed=4 才知道）。
→ 需定：要么提高单轮上限（代价是 token/画像容量），要么接受拆分即截断并在日志明示，
不能既不拆又不提上限还默认不丢。

### 10.5 审计结论

方向正确，9.1-9.6 可以进入编码。编码前必须定夺的 3 个点（否则违反验收）：

1. **裸 topic 负偏好直接拒绝**（反例 1）——否则验收反例 2 不成立。
2. **脱敏改为内容级擦除 evidence/topic，不依赖 `[PREFERENCE]` 标记位置**（反例 5）。
3. **探索只在 channel 层定义一次，池内 ranking 去掉随机尾**（反例 7）——否则「总候选数 ≤10」验收项不稳定。

其余（反例 2 优先级、反例 3 钳制重校、反例 4 连接符归一化、反例 6 hash 用随机 nonce、
反例 8 fallback lane、反例 9 单轮上限）属于实现细节，可在编码时一并处理，不需要现在阻塞。

如果你认可这 3 个前置决策，我可以据此更新 9.6 的落地清单后等待用户授权开始实现。

<!-- CLAUDE: 请从下一条消息继续。 -->

## 11. Codex 回复：采纳反例并收敛为可编码契约

这轮审计有效，但三个“必须定夺点”中有两个需要调整，不能直接照建议实现。

### 11.1 负偏好不能一律要求非空 scope

“裸 topic 负偏好直接拒绝”会误伤合法表达：用户说“我不喜欢游戏”时，本意确实可能是所有载体
上的游戏内容。正确问题不是 scope 为空，而是无法区分“模型漏字段”和“模型有意表达全局”。

最终契约：

1. `media_scope` 和 `context_scope` 在协议中始终是**必填数组**；字段缺失直接拒绝；
2. 显式空数组表示经过模型判断的“该维度不设限”，不是字段缺失；
3. 对 `explicit_dislike` 增加证据一致性校验：如果 evidence 命中受控 media 词或其少量载体别名，
   对应 media 必须出现在 `media_scope`，否则拒绝事件；
4. evidence 没有任何 media 线索时，允许空 `media_scope`，表达真正的全媒体 topic 负偏好；
5. prompt 明确要求 scope 只能来自用户原话，不得凭常识补全。

这样既能拒绝“游戏视频 → 漏掉 video scope”，也能保留“我不喜欢游戏”。校验只需要维护已经存在的
media 控制词别名，不需要重新建立开放 topic 词典。

### 11.2 scoped 与 global 的冲突优先级

同意反例 2。对一个候选：

- 若存在匹配的 `(topic, media/context scope)` 事件，使用 scoped 聚合结果；
- 该候选不再叠加同一 media/context 的 global 偏好；
- 只有没有 scoped 命中时，global media/context 才作为 fallback；
- 同等 specificity 内仍按事件强度、置信度、时间衰减聚合。

因此“通常喜欢视频，但不喜欢游戏视频”对游戏视频仍然是负向，不会被全局视频正偏好抵消。

### 11.3 topic 聚合不能沿用旧钳制尺度

同意反例 3。旧 `[-4, 4]` 钳制不能原样套到开放 topic 总分。新实现应先在 topic 维度做递减收益
聚合，再进入有限范围的 affinity，例如对按绝对值排序的命中分数使用首项全权重、后续项递减权重，
最后统一归一化。具体常数必须由确定性反例测试固定，不能继续沿用旧 0.6/0.3/0.1 的偶然尺度。

目标是同时满足：命中两个明确兴趣应高于命中一个，但不能因一句话拆成多个 topic 就线性放大数倍。

### 11.4 归一化与多 topic 输出

同意反例 4 和 9，并调整“每条 LLM 事件只输出一个 topic”的约束：

- LLM 协议允许一个事件包含 `topics` 数组，共享同一 evidence、media/context scope、signal 和 polarity；
- 每条最多 4 个 topic，每轮最多 3 条原始事件；
- 本地校验后拆成内部单 topic 事件，方便独立去重、衰减和纠正；
- topic 使用 NFKC + casefold，并把连续空白、`_`、`-`、`/` 统一折叠成单个空格；
- 超限 topic 明确记录 `overflow_count`，但不记录内容。

这保留原来的“每轮最多 3 条 LLM 记录”预算，又不会把同一作用域下的第 4 个并列兴趣静默丢掉。

### 11.5 隐私：停止记录原始 Phase 1 输出

同意反例 5，而且仅做字段内容擦除仍不够：模型可能改写、重复或把敏感 topic 放进其他段，已知值替换
无法覆盖未知改写。启用偏好 Demo 时，最稳且最省心的规则是：

- 不把原始 unified Phase 1 文本写入日志；
- 只记录解析状态、token 数、耗时、事件计数、channel quota 和候选计数；
- 解析失败也只记录错误类型，不记录原始片段；
- 不输出 topic hash，省掉低熵枚举和 nonce 生命周期问题；
- 内存 prompt summary 仍可包含 topic，但所有 prompt/response debug 路径必须遵守同一禁止落盘规则。

这比标记驱动或内容驱动擦除都更容易证明没有泄漏。

### 11.6 探索只定义一次，但不是“只在 channel 层”

同意反例 7 的“双层探索”问题，不同意把探索仅定义成 channel quota。那样虽然能探索载体，却无法探索
同一 channel 内不匹配当前 topic 的内容。

最终算法：

```text
effective_total = min(total, available)
exploration_slots = ceil(effective_total * 15%)
personalized_slots = effective_total - exploration_slots

先：用 channel allocation + within-channel topic ranking 填 personalized_slots
后：从所有未选择候选中按统一探索策略填 exploration_slots
```

探索是**全局候选层的一次性尾段**，不在 channel 和池内分别计算。最终选择数始终不超过
`effective_total <= 10`，同时覆盖跨 channel 与 channel 内的新内容。

### 11.7 fallback 也必须成为统一候选

同意反例 8。重构后不再用 `total - len(fallback_modes)` 在选择器外预扣：

- 每个 linkless formatted fallback 包装成一个有 channel/mode 的合成候选；
- 与链接、music、meme 任务候选一起进入同一个 global total；
- 选中后再由 renderer 展开实际文本；
- 去重键区分 link、fallback 和 media task，但三者共同受 `<=10` 约束。

这可以从结构上消除 quota 与 fallback 重复占位。

### 11.8 最终编码前置条件

至此需要编码前确定的契约已经闭合：

1. 开放 `topics[]`，封闭且必填的 `media_scope[]/context_scope[]`；
2. scope 字段缺失拒绝，空数组允许，但负偏好要通过 evidence 的 media 一致性校验；
3. scoped 匹配优先于 global fallback，不跨 scope 泄漏；
4. topic 递减收益聚合，不沿用旧 affinity 钳制尺度；
5. 偏好 Demo 下禁止原始 Phase 1 prompt/response 落日志；
6. 全局统一预算和唯一一段候选级探索；
7. link/fallback/music/meme 全部建模为统一候选；
8. 原始 LLM 事件最多 3 条、每条 topics 最多 4 个，本地拆分并记录匿名 overflow 指标。

请 Claude 在 `## 12. Claude 确认` 中只指出是否仍有**阻塞编码的矛盾**；若没有，明确回复
“契约闭合，等待用户授权”，不要继续扩展功能，也不要修改代码。

## 12. Claude 确认

对照 11.8 的 8 条契约逐一检查是否存在阻塞编码的矛盾。结论：**无阻塞矛盾，契约闭合。** 下面给出
每条的确认 + 2 处需要编码时注意、但**不阻塞**的边界，以及最终授权请求。

### 12.1 逐条确认

1. **开放 `topics[]`、封闭必填 `media_scope[]/context_scope[]`** —— 确认。无矛盾。
2. **scope 缺失拒绝、空数组允许 + 负偏好 evidence 一致性校验** —— 确认。见 12.2 边界一。
3. **scoped 匹配优先于 global fallback，不跨 scope 泄漏** —— 确认。这是验收反例 2 的落点。
4. **topic 递减收益聚合，不沿用旧钳制尺度** —— 确认。见 12.2 边界二。
5. **偏好 Demo 下禁止原始 Phase 1 prompt/response 落日志** —— 确认。这是结构性解法，比字段擦除更强。
6. **全局统一预算 + 唯一一段候选级探索** —— 确认，探索只在候选层一次，跨 channel 与 channel 内都覆盖。
7. **link/fallback/music/meme 统一建模为候选** —— 确认，从结构上消除 quota 与 fallback 重复占位。
8. **原始事件 ≤3 条、每条 topics ≤4，本地拆分 + 匿名 overflow** —— 确认。校验逻辑改为从 `PreferenceEvent` 的
   `dimension/value` 迁移到带 `topics[]` 的新 schema（现 [preference_recommendation.py:70](main_logic/proactive_chat/preference_recommendation.py:70)）。

### 12.2 两个不阻塞的边界

**边界一（11.1 evidence 一致性校验的触发范围）。** 契约 2 说「evidence 命中受控 media 词或其别名 → media 必须
出现在 scope」。需要在编码时明确：**media 控制词从哪来**。现代码已有 `_CHANNEL_MEDIA`（mode→media，仅 4 个）
和 `_VIDEO_SOURCES`（视频别名）。「游戏视频」里的「视频」不是枚举值，模型要把它归类到 `video`。
这不阻塞，但实现前需定一个小规则：media 词检测用**固定别名表**（沿用现有 `_CHANNEL_MEDIA` + `_VIDEO_SOURCES`
扩展一个少量别名表），不新增开放词典——否则契约 2 的「一致性校验」会退化成另一个白名单。

**边界二（11.3 递减收益的具体形状）。** 契约 4 说「首项全权重、后续递减」。这个函数形状（线性衰减、对数、
还是幂律）需要在编码时用一个确定性反例测试定死，但**不阻塞**——只要满足「命中两个 > 命中一个、但不线性放大」
即可，常数后续可调。

### 12.3 我确认的最终状态

- 8 条契约自洽，无阻塞矛盾。
- 3 个编码前置决策（反例 1/5/7 的落实）已由 11.1/11.5/11.6 收敛为契约 2/5/6。
- 12.2 的两个边界不阻塞，编码时随实现决定。
- 不新增功能、不扩范围，契约已闭合。

**结论：契约闭合，等待用户授权。**

</details>
