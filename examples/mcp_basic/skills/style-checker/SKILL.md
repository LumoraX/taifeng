---
name: style-checker
description: 代码风格规则参考资料
version: 1.0.0
type: atomic
scripts:
  - name: lookup_segment
    path: scripts/lookup_segment.sh
    language: shell
    timeout_seconds: 5
    description: 输入规则编号（R1..R10）返回中文规则全名与示例
    args_schema:
      type: object
      properties:
        segment:
          type: string
          description: 规则编号，必须形如 R1..R10
          pattern: "^R([1-9]|10)$"
      required: [segment]
---

# 代码风格规则

## 命名

- **Python**：snake_case 函数 / PascalCase 类 / UPPER_SNAKE 常量
- **TypeScript**：camelCase 函数 / PascalCase 类型 / UPPER_SNAKE 常量

## 关键阈值

- 函数行数 ≤ 80 行（硬红线）
- 圈复杂度 ≤ 10
- 文件行数 ≤ 800 行

## 规则编号

R1..R10 是常用规则编号。**精确查询**推荐调:

    run_script(skill_id="style-checker", script_name="lookup_segment", args={"segment": "R2"})

会返回该规则的中文全名 + 示例（如 "R2: 函数行数 ≤ 80 行"）。

## 常用术语

- **DRY**：Don't Repeat Yourself
- **YAGNI**：You Aren't Gonna Need It
- **SOLID**：单一职责 / 开闭 / Liskov / 接口隔离 / 依赖倒置
