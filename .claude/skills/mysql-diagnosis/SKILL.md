---
name: mysql-diagnosis
description: 指导已登记 MySQL 实例的人工接入准备、只读验证和按需巡检。
---

## 首次接入：由人配置

1. 确认 MySQL 实例的版本、地址、端口、监控机的来源地址，以及需要查看哪些业务库。当前项目通过 PyMySQL 从 Agent 所在机器直连 MySQL，不通过 SSH 转发。
2. 由数据库管理员人工创建专用监控账号并授权；限制账号的来源地址，只授予本次监控所需权限。当前 `check_mysql` 查询实例状态、全局变量和 `information_schema` 中可见的库表元数据，不读取业务表记录。若用户需要创建账号的 SQL，先确认版本、来源地址和需要覆盖的库，再给出供管理员审阅和执行的命令；不要声称已替用户执行。
3. 由用户把连接地址、端口、账号和密码填入项目 `.env` 中对应的 `MONITOR_MYSQL_*` 变量，并确认资产清单已登记 MySQL 服务。不要要求用户在对话中发送密码，也不要把密码写入 Skill、代码、资产清单或回答。
4. 配置后调用 `check_mysql` 验证。连接成功不等于能看见所有库表；如果 `metadata_error` 存在或预期的库表不可见，应请管理员检查授权范围。此工具不验证账号实际拥有哪些权限。

## 日常巡检：由 Agent 只读执行

1. 用户询问当前 MySQL 状态、库、表或视图时，对已登记资产调用 `check_mysql`。资产不明确时先调用 `list_assets`。只有用户要求对比历史时才调用 `observation_history`；历史结果不能代替本次查询。
2. 查看 `ok`、`checked_at`、`error` 和 `metadata_error`。`ok=false` 仅表示本次检查失败，不能直接断言 MySQL 宕机；工具只给出错误类型时，不要猜测唯一原因。
3. 根据用户问题选择相关指标：连接数与 `max_connections`、运行线程、运行时间、查询和慢查询累计数、InnoDB 缓冲池命中率，以及可见业务库、表、视图和估算空间。`connection_usage_percent` 是当前连接数占最大连接数的比例；`Questions`、`Connections`、`Slow_queries` 等是累计计数，不是每秒速率；`read_only` 是服务器配置，不代表监控账号权限。
4. `information_schema` 只返回监控账号有权看到的对象；未列出某库或表，不等于它不存在。InnoDB 的 `estimated_rows` 和空间是估算值；表和视图明细各最多 50 项，检查 `tables_truncated`、`views_truncated`。不要将 `metadata_error` 当成业务库为空。
5. 用清楚的中文呈现实测值、基于数据的判断和必要的限制，注明 `checked_at`。容量按工具结果中的十进制 MB/GB 显示，不再自行换算；不要输出凭据。

本 Skill 只指导流程；它不会创建账号、授予权限、修改 MySQL 配置或扩大 `check_mysql` 的查询范围。
