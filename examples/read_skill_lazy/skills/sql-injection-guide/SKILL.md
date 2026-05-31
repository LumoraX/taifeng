---
name: sql-injection-guide
description: SQL 注入防护指南（被 read_skill 按需读取）
version: 1.0.0
type: atomic
---
# SQL 注入防护指南

核心原则：**永不拼接用户输入到 SQL 字符串**。

1. 参数化查询 / 预编译语句（`cursor.execute("... WHERE id=%s", (uid,))`）是首选。
2. ORM 的参数绑定默认安全；避免 raw SQL 拼接。
3. 最小权限数据库账号 + 输入白名单校验作为纵深防御。
4. 绝不用字符串格式化（f-string / % / +）拼 SQL。
