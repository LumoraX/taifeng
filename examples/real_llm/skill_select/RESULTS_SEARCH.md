# 选择基准结果快照（deferred 召回版 · search_skills）

> 本文件是 `bench_search.py`（deferred 召回版）的结果台账，与 `RESULTS.md`（inline 基线）成对照。
> 两实验唯一变量是「child 暴露方式」：inline 全量内联 vs deferred 先 `search_skills` 召回再 `call_skill`。
>
> ⚠️ **以下数值为占位模板，待集成者真实跑 `bench_search.py` 后填入——严禁造假数据。**
> 跑法：`PYTHONPATH=src python examples/real_llm/skill_select/bench_search.py`（默认 sample=40，
> 全量加 `--sample 210`）。请与 `RESULTS.md` 用**同 provider / model / 任务集**对照，否则不可比。

## <待真实跑填入：日期> · <待填：provider · model> · <待填：样本数> 任务

- 候选规模：**210 个 skill**（router 走 deferred，prompt **不**内联 child）
- 样本：<待真实跑填入> 条
- 每任务 2 步：`search_skills` 召回 → `call_skill` 选定（deny `Skill(*)` + `max_iterations=2`）

**总准确率：<待真实跑填入> / <待填> = <待填>%**（未选出 <待填> 条；召回触达率 <待填>%；耗时 <待填>s）

| domain | acc | | domain | acc |
| --- | --- | --- | --- | --- |
| data | <待填> | | office | <待填> |
| ops | <待填> | | content | <待填> |
| analytics | <待填> | | dev | <待填> |
| doc | <待填> | | | |

### 与 inline 基线（RESULTS.md）对照

> 待真实跑后填入：相对 inline 91.9%（193/210, deepseek-v4-flash 全量）的差异。
> 预期观察点（**仅假设，待数据证实/证伪**）：
> - deferred 是否把「在 200+ 里硬选」降为「在召回 top-K 里选」从而改善**语义近邻误选**；
> - 召回触达率（模型是否乖乖先 search 再 call）——若部分任务跳过 search 直接拒答，会拉低准确率；
> - 召回是否漏召正确目标（正确 skill 没进 top-K → 模型无从选对），定位是召回精度问题还是选择问题。

### 错选 / 未选出形态

> 待真实跑后填入（参照 RESULTS.md 的形态分类：语义近邻误选 / 跨域重复 / 只搜不调 / 召回漏召）。

### 结论

> 待真实跑后填入。
