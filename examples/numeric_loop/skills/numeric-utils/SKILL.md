---
name: numeric-utils
description: 数值调谐公式与术语参考
version: 1.0.0
type: atomic
---

# 数值调谐参考

## 术语

| 名称 | 含义 |
| --- | --- |
| **current** | 当前数值 |
| **target** | 目标数值 |
| **delta** | 本轮施加的扰动量(带方向 + 随机噪声) |
| **new** | `current + delta` |
| **gap** | `\|new - target\|` —— 距离目标剩余距离 |
| **收敛** | `gap < 0.5` |

## 公式（apply_delta 内部）

```
direction = sign(target - current)
magnitude = uniform(0.3, 1.5) * |target - current| * 0.4
delta     = direction * magnitude
```

平均步长 = 40% 距离,但 0.3–1.5 倍随机系数 → **有概率 overshoot**。
overshoot 后下一轮方向反转,继续收敛。这就是「震荡回归」。

收敛通常需 5-10 轮。
