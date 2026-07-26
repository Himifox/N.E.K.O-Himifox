# Recommendation 多用户知情内测操作手册(M1-C)

> 状态:手册就绪,招募与采集未开始
> 立项授权:2026-07-26(成熟化计划 M1-C,见状态检查分支
> `docs/records/recommendation-maturity-plan-2026-07-26.md`)
> 边界约束:本手册只涉及**只读 Shadow 数据采集**,不改变任何生产行为;
> 一切以 [`RECOMMENDATION_CURRENT_SCOPE.md`](./RECOMMENDATION_CURRENT_SCOPE.md) 的当前边界为准。

## 1. 目的与范围

打破"全部数据来自单一开发者"的证据限制,获得首个多用户 Shadow cohort:

- 参与者:5–10 名**知情同意**的志愿者;
- 周期:每人正常使用 7 天;
- 目标(按**合计**计,不苛求每人):≥200 条推荐决策 observation、≥50 条可归因显式反馈;
- 模式:全程 shadow——推荐系统只观察打分,不改变任何搭话行为、内容或时机;
- 附带观察目标:activity 覆盖(特别关注 `focused_work` 是否首次出现;`unknown` 不计个性化覆盖)。

## 2. 知情同意说明(模板,发给每位参与者)

> 你将参与 N.E.K.O 主动推荐系统的影子数据采集,为期 7 天。
>
> **会记录什么**:主动搭话发生时的候选来源、候选 ID、排序分数、活动状态
> (如 idle/chatting)、时间间隔计数,以及你对主动搭话的显式反应
> (回复了/播放了音乐/关闭了)——全部经白名单脱敏,只有聚合特征。
>
> **绝不记录什么**:你的对话原文、屏幕内容、截图、浏览的 URL、
> 任何 token/cookie/账号信息。
>
> **数据用途**:仅用于离线测试台架分析(评估推荐质量),不用于训练模型、
> 不上传第三方、不公开原始数据;分析产物只含聚合统计。
>
> **你的权利**:可随时退出;退出后可要求删除你贡献的原始日志
> (未进入 immutable freeze 的部分直接删除;已冻结批次将整批标注废弃、
> 不再用于新分析)。
>
> 同意请回复"同意参与",并注明希望使用的参与者代号(将作为 cohort 标记)。

## 3. 参与者环境配置

在启动 N.E.K.O 前设置以下环境变量(变量名与取值与
`config/proactive_settings.py` 一致,已核对):

```bash
# 保持默认 shadow(不要设 active_source)
PROACTIVE_RECOMMENDATION_MODE=shadow
# 打开两个采集日志(默认 off,必须显式开启)
PROACTIVE_RECOMMENDATION_OBSERVATION_LOG=jsonl
PROACTIVE_RECOMMENDATION_FEEDBACK_LOG=jsonl
# 打开脱敏 review context(供后续人工评审用)
PROACTIVE_RECOMMENDATION_REVIEW_CONTEXT_MODE=shadow_review
# 以下保持默认,不要修改
# PROACTIVE_RECOMMENDATION_TUNING_MODE=off(默认)
```

**配置自检**:启动后访问 `GET /api/proactive/recommendation/summary`,
确认 `log_enabled` 为 true 且 `missing` 不再报日志缺失;主动搭话功能按参与者
平时习惯开启即可(至少开启一个内容来源,否则不会产生决策)。

日志落盘位置:生产配置目录下的
`proactive_recommendation_observations.jsonl` 与
`proactive_recommendation_feedback.jsonl`。

## 4. 采集期规范(发给参与者)

1. 正常使用即可,不需要刻意与主动搭话互动——真实反应才是有效数据
   (愿意回就回,不想理就不理,想关就关);
2. 不要手工编辑或删除上述两个 jsonl 文件;
3. 采集期间不要修改推荐相关环境变量;
4. 7 天结束后按第 5 节回收数据;中途想退出直接说,已采数据按第 2 节承诺处理。

## 5. 数据回收与导入(操作者执行)

1. **参与者侧导出**:在参与者机器上运行
   `python tests/testbench/tools/export_recommendation_shadow.py --output <代号>.json`
   (必要时用 `--config-dir` 指定生产配置目录;默认 `--limit 1000` 满足单人 7 天规模)。
   该工具产出**已脱敏**的导入包并附带 `audit_shadow_dataset` 审计结果——
   传输的是脱敏包,原始日志不离开参与者机器;
2. **回收**:参与者仅提交导出的 JSON 包(任何私密渠道均可);
3. **导入**:操作者在 testbench 中经 `POST /datasets/import` 导入(内部走生产
   sanitizer + timing 漂移拒绝门;单包上限 1000 条),数据集命名带参与者代号,
   保持 per-user 可区分;
4. **冻结**:合计达标后用现有 freeze 工具(如
   `tests/testbench/tools/freeze_recommendation_v2_cohort.py`)固化多用户 cohort,
   记录 SHA-256;freeze 后不可变,后续分析只引用 freeze;
5. **门禁**:分析前跑常规 readiness 审计;不达标就如实报告缺口,不降门槛。

## 6. 红线(与当前边界文档一致)

- 不写生产配置、权重、interval、scheduler 或 tuning;
- 不为重新分批而删除/归档/轮转参与者日志——批次只用 immutable freeze/cutoff 划分;
- `unknown` activity 不计入个性化覆盖;`focused_work` 样本不足时只报告限制、不外推;
- 参与者数据不得用于本手册范围之外的任何分析,除非另行立项并重新取得同意;
- 本次采集**不构成**任何候选评估、Shadow 重排或生产变更的授权——那些仍需按
  §7 流程单独申请。

## 7. 完成判据

- ≥5 名参与者完成 ≥5 天采集;
- 合计 ≥200 决策 observation / ≥50 可归因显式反馈,导入与冻结完成;
- 产出:多用户 freeze(含 SHA-256)+ activity 覆盖报告 + 每参与者数据量清单。

达成后,该 cohort 成为 M2(个性化验收)与 M3(决策门建模)的共同数据基础。
