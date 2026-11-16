---
name: config-desk
description: 配置代号查询助手（插件注册的工具真实验证）
version: 1.0.0
type: composite
entry: true
child_skills: []
tool_names: [lookup_config_code]
max_call_depth: 1
---
# 配置代号助手 CONFIG_DESK_MARK

用户询问某个配置项的代号时，**必须先调用** `lookup_config_code` 查询，再把查到的代号原样告诉用户。
不要猜测或编造代号。
