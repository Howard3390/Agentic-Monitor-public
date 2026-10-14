---
name: snmp-diagnosis
description: 解释清单中 Linux 主机的一次 SNMP 按需巡检结果。
---

用户询问当前状态时使用 `check_snmp`，不能用历史值替代当前查询。资产不明确时先用 `list_assets`。`ok=true` 只表示本次 SNMP 查询取得了至少一项指标；仍需检查 `warnings`、空列表和空值。

CPU 利用率由过去约一分钟的空闲率计算。工具返回给模型的容量字段为 `*_size`，已按十进制 MB/GB 格式化，无需再次换算。`memory.available_size` 是 Net-SNMP `memSysAvail` 提供的可用内存估计，`free_size` 只是空闲内存。`disks[].available_size` 是挂载点剩余容量；来自 `hrStorage` 的条目没有 inode 使用率，也不能声称覆盖所有磁盘。`interfaces` 是 目标 Linux 主机可见的网卡，网络速率来自约一秒内两次累计字节数采样；不能把累计字节数说成实时速率。

查询失败可能来自凭据、UDP 161 防火墙、网络、服务或 OID 权限，不能直接断言设备宕机。回答需给出 `checked_at`，将实测值和推断分开。虚拟机或容器中的 Linux 指标不能代表宿主机整体指标。
