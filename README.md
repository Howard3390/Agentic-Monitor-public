# Agentic Monitor

对话式按需监控项目。用户在终端或 Streamlit 页面提问，模型根据问题选择只读工具，Python 执行 SNMP、Docker 或 MySQL 查询，再把实测结果交给模型组织中文回答。项目不运行 Zabbix 服务，也不做后台定时采集。

## 主要能力

- **受控工具调用**：`list_assets`、`check_snmp`、`check_docker`、`check_mysql` 和 `observation_history` 均在 `app.py` 明确注册。查询目标只能来自资产清单，模型不能指定任意地址、OID 或系统命令。
- **按需加载技能**：SNMP 和 MySQL 诊断流程位于 `.claude/skills/`。对应检查执行前，Agent 先调用 `load_skill` 读取流程。
- **实时查询与历史记录**：当前状态通过工具重新查询；历史观测只用于对比。结果写入本地 `.data/`，不会包含在公开仓库中。
- **多种只读探针**：SNMP 获取 Linux CPU、内存、分区和网卡信息；SSH 查询 Docker 版本与容器状态；PyMySQL 查询实例状态和监控账号可见的库表元数据。
- **界面与终端**：`web_app.py` 提供对话和最近观测页面，`app.py` 提供单次探测、资产列表与交互式问答。

```text
用户问题 → 模型选择工具 → 可选 load_skill → 校验资产和参数
                                  ↓
                        SNMP / Docker / MySQL 只读查询
                                  ↓
                        记录观测 → 模型生成中文回答
```

## 安装与配置

在 Python 环境中执行：

```powershell
python -m pip install -r requirements.txt
```

将 `.env.example` 复制为 `.env`，将 `inventory.example.json` 复制为 `inventory.json`，然后填写自己的模型、主机和只读监控凭据。两个真实配置文件都被 `.gitignore` 排除。

```powershell
Copy-Item .env.example .env
Copy-Item inventory.example.json inventory.json
```

`MODEL_BASE_URL` 应是使用 HTTPS、兼容 OpenAI Chat Completions 的接口基地址；`MODEL_NAME` 填该接口支持的模型名。示例资产使用文档保留地址 `192.0.2.20`，只用于说明清单结构。运行前必须把 `inventory.json` 中的地址改成你有权监控的实际主机，并使 SSH/MySQL 的 `*_HOST` 与清单 `host` 一致。SNMP 使用只读 community；MySQL 账号由管理员人工创建，只授予所需的查询权限。

Docker 检查使用 SSH。目标主机密钥需预先存在运行 Agent 用户的 `~/.ssh/known_hosts`；未知主机密钥会被拒绝。MySQL 检查直接连接数据库，不通过 SSH 转发。程序不会创建账号、修改设备或读取业务表记录。

## 运行

查看资产或做单次检查，无需模型：

```powershell
python app.py --list
python app.py --probe example-linux
python app.py --probe example-linux --service docker
python app.py --probe example-linux --service mysql
```

使用模型问答：

```powershell
python app.py "这台主机的 CPU 和内存情况怎么样？"
python app.py
```

启动网页：

```powershell
python -m streamlit run web_app.py --server.address 127.0.0.1
```

`--probe` 默认执行 SNMP 检查。网页仅在提问时发起新查询，刷新页面不会自动采集。资产清单未配置时，网页会提示复制示例清单。

## 项目文件

- `app.py`：工具注册、技能加载、模型工具调用循环和终端入口。
- `inventory.py` / `inventory.example.json`：资产清单校验与脱敏示例。
- `probes.py`、`snmp_host.py`、`service_probes.py`：只读探针。
- `memory.py`：本地观测记录。
- `units.py`：容量单位格式化。
- `web_app.py`：Streamlit 对话与观测页面。
- `.claude/skills/`：按需加载的 SNMP、MySQL 诊断流程。
- `tests/`：模拟模型和探针的基础测试。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试使用示例地址或模拟结果，不会连接真实设备或调用模型。公开版没有附带真实 `.env`、资产清单、观测记录或个人路径；上传前可用 `git status --short` 核对待提交文件。
