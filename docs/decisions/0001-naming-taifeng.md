# ADR 0001: 项目命名为 泰逢 (Taifeng)

- 状态：Accepted
- 日期：2026-05-23
- 作者：项目发起人

## 背景

项目最初命名为 `Loom`（织机），取其「把 skill / 对话 / 工具 / turn 织成一段连贯输出」的工艺隐喻。设计推进至「LLM Agent OS / 微内核」框架后，意识到「织造」隐喻不准确：

- 织造强调「**编织成品**」，但 OS 内核不产出成品，而是**调度让事得以发生**
- 织造缺少「**看不见的流**」（token / event / cache / cancellation）的意象
- 织造没有「**调度 / 守护 / 中枢**」的神格

需要一个更精准对应「LLM 微内核」语义的命名。

## 决策

**采用 `泰逢 (Taifeng)`**，山海经·中山经神兽。

## 原典出处

```
《山海经·中山经》：
"和山... 吉神泰逢司之，
 其状如人而虎尾，
 是好居于萯山之阳，出入有光。
 泰逢神动天地气也。"
```

## 隐喻映射

| 原文 | 字面意 | 映射到本系统 |
| --- | --- | --- |
| **吉神泰逢司之** | "吉神" 泰逢主持 | `AgentEngine.run()` 主 actor；"司"字呼应调度域核心主宰 |
| **其状如人而虎尾** | 人形 + 虎尾（不纯神不纯兽） | 简洁接口（人形）+ 底层真实能力（虎尾）；engine 暴露 `submit / subscribe`，底下是 cancellation token / RwLock / cache tracker |
| **居于萯山之阳** | 居住在山的南面（阳面） | API 是 sunlit 的，没有藏在内核黑盒；协议清晰、可测、可替换 |
| **出入有光** | 行动伴随光 | Telemetry 是 first-class —— 每次 turn / dispatch / cache_break / compaction 都有 EventMsg 这道"光" |
| **神动天地气也** | 驱动天地之"气" | 调度 token flow / event bus / cache pool / cancellation tree —— 看不见的流 |

## 「气」是核心

中国宇宙观里「**气**」的属性：**流动 / 不可见但有效果 / 阴阳对偶 / 循环**。

对应本系统里看不见但决定一切的流：

```
天地气 = {
    token_flow:        LLM 进出 prompt window 的字节流
    event_flow:        Submission / EventMsg 总线
    cache_flow:        prompt cache hit/miss 的命中率波动
    cancel_flow:       CancellationToken 沿父子树传播
    call_flow:         skill 之间的 call_skill 调用图
}
```

Taifeng 不"做"什么，它**让事得以发生** —— 这是 OS 调度器的本质。

> 类比：Unix 内核不"写文件"、不"发包"，它**让进程能写文件、能发包**。
> 泰逢不"行雨"、不"造物"，他**动气** —— 让风雨万物自然发生。

## 「吉神」却能为乱 —— 调度器的双刃

《竹书纪年》记载夏朝孔甲在萯山打猎，遇泰逢，**泰逢起大风让孔甲迷路**。

山海经里很多神兽是固定凶 / 固定吉，但**泰逢明确写"吉神"，又能致乱**。

对应：
- 配错 `max_call_depth` → fork 炸弹
- 压缩策略错 → cache 雪崩
- 派发白名单忘配 → 静默失败
- 取消 token 漏传 → 僵尸 task

**吉与不吉之间只隔一个 `DispatchPolicy.check()`**。所以代码层那些守护（环检测 / 深度限制 / 白名单 / cancellation 父子化）不是冗余，是让"吉神"始终为吉的护身符。

## 隐喻定位

|  | 泰逢 Tàiféng |
| --- | --- |
| 司什么 | 天地气（能量 / 流） |
| 形态 | 人形虎尾（半人半兽） |
| 居所 | 萯山之阳 |
| 隐喻 | Flow orchestrator —— 调度 token / event / cache / cancellation 的"看不见的流" |
| 对应层 | infra 引擎（本项目 Taifeng） |

## 命名标语

中文：
> **泰逢动天地气。**

技术化版本：
> *Taifeng: the orchestrator of invisible flows that bring LLM agents to life.*

## 实操属性

| 维度 | 值 |
| --- | --- |
| 中文名 | 泰逢 |
| 罗马字 | Tàiféng |
| 包名 | `taifeng` |
| 仓库 | `infra/taifeng` |
| 山海经亲缘 | ✅ 中山经 |
| 拼音长度 | 7 字符 |
| 品牌冲突 | 极低（中文古籍冷门词，无明显商业占用） |

## 历史

本项目最初命名讨论详见对话记录与早期文档（`Loom` 时期）。`Loom` 这个候选名以「织机 / cache 经线 + tail 纬线」的隐喻起步，但在系统定位明确为「LLM 微内核」后，「织造」语义不够精准。**Loom 的全部设计内容（架构、ADR 0002–0006、源码骨架）保留**，仅更名为 Taifeng。

## 否决的候选

| 候选 | 否决理由 |
| --- | --- |
| Loom（原名） | 织造隐喻偏成品输出，与"调度让事发生"的内核语义不符 |
| 羲和 Xīhé | 织日轮的"织造"专家；与「曦和大模型」品牌可能冲突 |
| 烛龙 Zhúlóng | 时间循环之神，气场过大，"千里巨龙"叙事偏宏大 |
| 嫘祖 Léizǔ | 蚕织文化母系，不是山海经；偏"创始者"非"调度者" |
| 英招 Yīngzhāo | "司帝之平圃 + 巡狩四海" 派发隐喻好；但"动天地气"更直击内核本质 |
| 太一 Tàiyī | 至高神，气场过大；非山海经直系 |
| 北辰 / 紫微 | 星象不是神兽；与"动天地气"内核语义距离过大 |
| 帝江 Dìjiāng | "浑沌" 是未分而非织成，方向反 |

## 相关

- [架构总览](../architecture/overview.md)
- ADR 0006「统一 Skill 模型」—— 删除 Agent 概念，确立 LLM-as-scheduler 范式（这是促成命名从 Loom 转向 Taifeng 的关键决策）
