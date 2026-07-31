# CountBot 代码库文档

> 版本 0.9.0 | Python FastAPI + Vue 3 | ~15万行 | 500+ 文件

---

## 目录

1. [项目概览](#1-项目概览)
2. [系统架构](#2-系统架构)
3. [Agent 模块](#3-agent-模块)
4. [Providers 模块](#4-providers-模块)
5. [Tools 模块](#5-tools-模块)
6. [Channels 模块](#6-channels-模块)
7. [MCP / Wiki / Cron / Config / WebSocket / 前端](#7-其他模块)

---

## 1. 项目概览

### 1.1 是什么

CountBot 是一个面向中文用户的 AI Agent 框架与运行中枢，连接 LLM、IM 渠道、工具、记忆和多人协作，让 AI 具备长期运行和跨入口执行能力。

### 1.2 核心能力

| 能力 | 说明 |
|---|---|
| Agent Loop | ReAct 推理循环：思考 → 工具调用 → 结果反馈 → 迭代 |
| Multi-Agent | Pipeline（流水线）、Graph（依赖图）、Council（多视角审议） |
| Tool System | 17+ 内置工具 + MCP 动态工具，审计日志 + 会话隔离 |
| LLM Providers | 23 个供应商，OpenAI/Anthropic 双实现，Key 轮换 |
| IM Channels | 8 个渠道，监督式自动重连 + 多账号支持 |
| Memory | 行式文件记忆 + 会话持久化 + 自动摘要 |
| Cron | 定时任务 + 心跳主动问候 |
| MCP | Model Context Protocol 客户端（stdio/SSE/HTTP） |
| Wiki | BM25 全文搜索知识库 |

### 1.3 目录结构

```
CountBot/
├── backend/                          # Python FastAPI 后端 (~6.1万行)
│   ├── app.py                        # FastAPI 入口 + 生命周期管理
│   ├── database.py                   # SQLAlchemy 异步 + SQLite
│   ├── version.py
│   ├── api/                          # 15 个 REST 路由
│   ├── models/                       # 8 个 ORM 模型
│   ├── modules/
│   │   ├── agent/                    # ★ Agent 核心 (loop/workflow/subagent/context/memory)
│   │   ├── providers/                # ★ LLM 供应商抽象
│   │   ├── tools/                    # ★ 工具系统 (registry + 17个工具)
│   │   ├── channels/                 # ★ IM 渠道 (base + 8个渠道 + handler)
│   │   ├── mcp/                      # MCP 客户端
│   │   ├── wiki/                     # BM25 知识库
│   │   ├── cron/                     # 定时任务调度
│   │   ├── config/                   # 配置管理 (Pydantic schema + DB loader)
│   │   ├── session/                  # 会话管理
│   │   ├── messaging/                # 消息总线 + 限流
│   │   ├── external_agents/          # 外部编码代理适配
│   │   ├── auth/                     # 认证中间件
│   │   ├── websocket/                # WS 广播
│   │   └── workspace/                # 工作空间管理
│   ├── ws/                           # WebSocket 连接处理
│   └── utils/                        # 工具函数
├── frontend/                         # Vue 3 + TypeScript (~7.9万行)
│   └── src/
│       ├── modules/   (chat/settings/tools/skills/mcp/wiki/memory/scheduler/teams/system)
│       ├── stores/    (10 个 Pinia store)
│       └── router/
├── config/                           # 默认配置
├── data/                             # 运行时数据
├── workspace/                        # 默认工作空间
├── start_app.py / start_dev.py / start_desktop.py
└── requirements.txt
```

---

## 2. 系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                 8 个 IM 渠道 (入站/出站)                          │
│   Telegram │ QQ │ 微信 │ 钉钉 │ 飞书 │ 微博 │ 企微 │ 小智       │
└────────────────────────────┬────────────────────────────────────┘
                             │ InboundMessage / OutboundMessage
                    ┌────────▼────────┐
                    │  ChannelManager │  监督式生命周期 + 指数退避重连
                    └────────┬────────┘  多账号支持 + 消息路由
                             │
                    ┌────────▼────────┐
                    │ EnterpriseQueue │  消息总线 + 去重
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │   ChannelMessageHandler     │  命令分发 + 会话管理
              └──────────────┬──────────────┘  流式输出 + 配置热加载
                             │
              ┌──────────────▼──────────────┐
              │       AgentLoop             │  ReAct 核心循环
              │    (agent/loop.py)          │  LLM ↔ 工具 迭代
              └──┬────────────┬─────────────┘
                 │            │
    ┌────────────▼──┐  ┌─────▼──────────┐
    │  LLMProvider  │  │  ToolRegistry  │
    │  (providers/) │  │  (tools/)      │
    │  OpenAI/Anthr │  │  17+内置+MCP   │
    └───────────────┘  └────────────────┘
```

### 2.2 启动顺序 (app.py lifespan)

```
1. init_db()                           # SQLite 初始化
2. config_loader.load()                # 从 DB 加载 AppConfig
3. _create_shared_components()         # Provider + Workspace + Memory + Skills + ToolRegistry
4. ChannelManager(config, bus)         # 初始化已启用渠道
5. ChannelMessageHandler(...)          # 消息处理器 (含 AgentLoop)
6. MCP 后台连接 (非阻塞)                # 连接已启用 MCP 服务器
7. channel_manager.start_all()         # 后台启动渠道
8. message_handler.start_processing()  # 后台启动消息循环
9. CronScheduler.start()               # 定时任务调度
10. ensure_heartbeat_job()             # 心跳任务注册
```

### 2.3 一条消息的完整旅程

```
用户发消息 (如 Telegram)
  → TelegramChannel._handle_message()    # 校验白名单，构造 InboundMessage
  → ChannelManager._on_inbound_message() # 转发到消息总线
  → EnterpriseQueue.publish_inbound()    # 入站队列 (含去重)
  → ChannelMessageHandler.handle_message()
      │ 1. 清理 @mention 唤醒词
      │ 2. 解析会话路由 (群聊/私聊, 多账号)
      │ 3. 命令检测 (/new, /help, /team...)
      │ 4. 获取或创建会话
      │ 5. 解析运行时配置 (Provider + 模型 + 性格)
      │ 6. 获取会话历史 (含自动摘要)
      └→ AgentLoop.process_message()
           │ 🔄 ReAct 循环 (最多 max_iterations 轮)
           │   ├─ ContextBuilder.build_messages() 构建上下文
           │   ├─ Provider.chat_stream() 流式调用 LLM
           │   ├─ 解析响应 (文本 / tool_calls / reasoning)
           │   ├─ 去重工具调用
           │   ├─ ToolRegistry.execute() 执行工具
           │   ├─ 追加 tool result 到 messages
           │   └─ 循环直到 LLM 返回纯文本 或 达到上限
           └→ yield 文本块 (流式)
  → 保存回复到 DB → 发布到出站总线 → Channel.send() → 用户收到
```

### 2.4 配置层级 (优先级从高到低)

```
会话级 (session_model_config / session_persona_config)   ← /m /p 命令设置
  └→ 团队级 (agent_team.model_config)                     ← 团队自定义 Provider/模型
      └→ 角色级 (persona: ai_name, personality...)        ← 全局性格
          └→ 全局默认 (config.model / config.persona)     ← Web 界面设置
```

### 2.5 关键设计决策

- **不用 LangChain**: 自研循环精确控制每次 LLM 调用，避免抽象过重
- **contextvars 而非实例变量**: `ToolRegistry` 的 session_id/channel 用 `contextvars` 存储，多协程安全
- **工具去重**: Agent Loop 和 Subagent 都对 LLM 的工具调用做去重 (按 tool_call_id 和签名)
- **Key 轮换**: 401/429 错误时自动切换备用 API Key，最多 3 次，只对认证/限流错误触发
- **渠道监督重连**: 每个渠道独立监督任务，指数退避重连 (5s→10s→...→300s)，稳定运行 60s 后重置

---

## 3. Agent 模块

> 路径: `backend/modules/agent/` | 16 个文件 | 核心大脑

### 3.1 文件清单

| 文件 | 类 | 职责 |
|---|---|---|
| `loop.py` | **AgentLoop** | ★ ReAct 核心循环，约 900 行 |
| `workflow.py` | **WorkflowEngine** | ★ 多智能体编排 (Pipeline/Graph/Council) |
| `subagent.py` | **SubagentManager** | 后台子代理创建/执行/取消/持久化 |
| `context.py` | **ContextBuilder** | 系统提示词组装 + 消息构建 |
| `memory.py` | **MemoryStore** | 行式文件记忆 (读/写/搜索/删除) |
| `personalities.py` | — | 12 个性格预设 |
| `prompts.py` | — | 系统提示词模板 |
| `skills.py/config/schema` | **SkillsLoader** | 技能文档加载与管理 |
| `heartbeat.py` | **HeartbeatService** | 主动问候逻辑 |
| `task_manager.py` | **CancellationToken** | 任务取消令牌 |
| `team_commands.py` | — | /team 命令解析 |
| `compactor.py` | — | 上下文压缩 (空文件，功能未独立) |
| `analyzer.py` | — | 消息分析 |

---

### 3.2 AgentLoop — ReAct 核心循环

**类:** `AgentLoop`  
**构造函数参数:** provider, workspace, tools, context_builder, session_manager, subagent_manager, model, max_iterations(25), max_retries(3), temperature(0.0), max_tokens(4096), thinking_enabled(True)

**process_message() 流程:**

```
process_message(message, session_id, context, ...)
  │
  ├─ 1. 设置工具注册表上下文 (session_id, channel, cancel_token)
  │
  ├─ 2. ContextBuilder.build_messages(history, current_message, ...)
  │      → messages = [system, ...history, user]
  │
  ├─ 3. _resolve_execution_runtime(model_override)
  │      解析 provider/model/temperature/thinking
  │      支持动态覆盖 (团队自定义模型)
  │
  ├─ 4. 迭代循环 (while iteration < max_iterations):
  │     │
  │     ├─ provider.chat_stream(messages, tools) → async for chunk
  │     │    ├─ chunk.is_content     → 累积文本 + yield (流式)
  │     │    ├─ chunk.is_reasoning   → reasoning_event_handler 回调
  │     │    ├─ chunk.is_tool_call   → 追加到 tool_calls_buffer
  │     │    ├─ chunk.is_done        → 记录 finish_reason
  │     │    └─ chunk.is_error       → _try_key_rotation() 尝试恢复
  │     │
  │     ├─ 工具调用去重 (按 tool_call_id)
  │     │
  │     ├─ 迭代执行每个工具:
  │     │    ├─ tool_event_handler 通知前端
  │     │    ├─ WebSocket 广播通知
  │     │    ├─ execute_tool() → ToolRegistry.execute()
  │     │    │    └─ 失败重试 max_retries 次
  │     │    └─ 结果追加为 tool role message
  │     │
  │     ├─ 特殊: workflow_run + prefer_direct_workflow_result
  │     │    └─ 直接用工作流结果作为最终回复
  │     │
  │     └─ 无工具调用 → 跳出循环
  │
  └─ 5. 保存到 session_manager + 审计日志
```

**两个入口:**
- `process_message()` — 流式 async generator (WebSocket / 渠道)
- `process_direct()` — 直接返回完整字符串 (CLI / Cron)

**_try_key_rotation() 逻辑:**
```
收到错误 → 判断是否认证/限流错误 (401/429/unauthorized/rate limit)
  → 检查轮换次数 < 3
  → 检查是否有多余 Key
  → KeyRotator.mark_key_failed() 标记当前 Key
  → create_provider(new_key) 创建新 Provider
  → 返回新 Provider (上层用新 Provider 重试当前迭代)
```

---

### 3.3 WorkflowEngine — 多智能体编排

**类:** `WorkflowEngine`  
**枚举:** `WorkflowMode(PIPELINE/GRAPH/COUNCIL)`, `SlotPhase(WAITING/ACTIVE/DONE/FAILED)`  
**数据类:** `AgentSlot` (slot_id, label, prompt_template, depends_on, condition, phase, output, error)

**三种模式:**

#### Pipeline (流水线) — `run_pipeline(goal, stages)`
```
Stage 1 → Stage 2 → Stage 3 → 最终输出
每个 stage 获得前序所有 stage 的输出作为上下文
底层通过 _invoke_agent() → SubagentManager 执行
```

#### Graph (依赖图) — `run_graph(goal, slots)`
```
        ┌──→ Node B ──┐
Node A ─┤              ├──→ Node D
        └──→ Node C ──┘

- 同层并行 asyncio.gather()
- DFS 环路检测 (_detect_cycle)
- 条件执行 (output_contains / output_not_contains)
- 上游失败 → 下游 Blocked by upstream failure
```

#### Council (多视角审议) — `run_council(question, members, cross_review=True)`
```
Round 1: 各成员独立分析 (并行)
Round 2: 交叉评审 (并行，每个成员看到其他人第1轮观点)
最终: 汇总所有观点 (保持独立，不做合成)
cross_review=False → 跳过第2轮
```

**_invoke_agent() 流程:**
```
SubagentManager.create_task(event_callback=_tool_event)
  → WebSocket 事件: workflow_agent_start
  → 工具事件: workflow_agent_tool_call / workflow_agent_tool_result
  → 流式: workflow_agent_chunk
  → 完成: workflow_agent_complete
```

---

### 3.4 SubagentManager — 子代理管理

**类:** `SubagentManager`, `SubagentTask`, `TaskStatus(PENDING/RUNNING/COMPLETED/FAILED/CANCELLED)`

**任务生命周期:**
```
create_task(label, message) → PENDING → task_id
execute_task(task_id) → RUNNING → asyncio.create_task(_run_task())
  ├─ _run_task_impl():
  │    ├─ 独立 ReAct 循环 (仅 5 个基础工具, max_iterations=15)
  │    ├─ 支持 team model_override
  │    ├─ 支持 cancel_token 中断
  │    └─ 每次工具调用后 _save_task_to_db()
  ├─ 超时保护 (默认 1200s)
  ├─ COMPLETED / FAILED / CANCELLED
  └─ done_event.set() (通知等待者)
```

**子代理 vs 主代理差异:**

| 维度 | 主代理 | 子代理 |
|---|---|---|
| 工具集 | 全部 17+ + MCP | 仅 5 个 (文件+Shell+Web) |
| 权限 | 完整 | restrict_to_workspace=True |
| 系统提示词 | 完整身份+技能+规则 | 简短: 专注任务, 不能联系用户 |
| 超时 | 无硬超时 | 1200s (可配置) |
| 取消 | cancel_token | cancel_token (级联) |
| 可再创建子代理 | 是 | 否 |

**核心方法:**
- `create_task()` — 创建任务
- `execute_task()` — 调度后台执行
- `cancel_task()` / `cancel_all_tasks()` — 取消
- `get_task()` / `list_tasks()` — 查询
- `get_stats()` — 统计
- `cleanup_old_tasks()` — 清理过期任务

---

### 3.5 ContextBuilder — 上下文构建器

**类:** `ContextBuilder`  
**构造函数参数:** workspace, memory, skills, persona_config

**build_system_prompt() 组装顺序:**
```
├─ 1. _get_identity()
│     ├─ 动态: 当前时间, 运行环境, 工作目录
│     ├─ persona_config → ai_name, user_name, 性格描述
│     ├─ CountBot 规则 (工具使用/安全准则/工作原则)
│     └─ 外部编码代理引导 (有 enabled profile 时)
├─ 2. 已激活技能 (always=true 的 skill, 完整文档内联)
├─ 3. 可用技能摘要 (名称+一行描述, 按需 read_file 加载)
└─ 4. 已激活团队 (_get_active_teams_section, 从 DB 查询)
```

**build_messages() 完整流程:**
```
build_system_prompt()
  + session_summary (历史摘要)
  + channel/chat_id/account_id 上下文
  + 团队调用提醒 (检测 @团队名)
  → [system message]
  + extend(history)     → 历史对话
  + _build_user_content(text, media)  → 用户消息 (媒体路径文本注入)
```

**性格来源优先级:**
1. `persona_config` 参数 (运行时覆盖)
2. `self.persona_config` (实例配置)
3. DB `Personality` 表 (用户自定义)
4. `personalities.py` 硬编码 (12 个兜底: grumpy, gentle, blunt, toxic, chatty, philosopher, cute, humorous, hyper, chuuni, zen, roast)

---

### 3.6 MemoryStore — 记忆系统

**类:** `MemoryStore`  
**存储格式:** `日期|来源|内容事项1；事项2；事项3`

```
# MEMORY.md 示例:
2026-02-15|web-chat|用户询问天气API方案；决定使用OpenWeatherMap；缓存策略选Redis TTL=3600s
2026-02-15|telegram|用户要求每天早上9点发送日报；已创建cron任务
```

**核心 API:**

| 方法 | 说明 |
|---|---|
| `append_entry(source, content)` | 追加一行记忆 |
| `search(keywords, match_mode="or"/"and")` | 关键词搜索 |
| `read_lines(start, end)` | 按行号读取 |
| `delete_lines(line_numbers)` | 按行号删除 |
| `get_recent(count=10)` | 最近 N 条 |
| `get_stats()` | 来源分布 + 日期范围 |

---

### 3.7 技能系统

**类:** `SkillsLoader`  
**加载源:** `workspace/skills/` 目录

```
优先级:
1. always=true 的技能 → 完整 SKILL.md 内联到系统提示词
2. 其他技能 → 仅注入名称+一行描述的摘要
3. LLM 需要时 → 调用 read_file 读取完整文档
4. 按文档说明 → 调用 exec 执行命令
```

设计哲学: 技能是文档而非硬编码函数 —— LLM 先理解文档再执行，避免扩展局限性。

---

## 4. Providers 模块

> 路径: `backend/modules/providers/` | 9 个文件 | LLM 供应商抽象

### 4.1 架构

```
create_provider() ← 工厂 (factory.py)
  │ 根据 api_mode 分派
  ├─ OpenAIProvider (openai_provider.py)    ← 兼容所有 OpenAI 协议服务
  └─ AnthropicProvider (anthropic_provider.py) ← Anthropic Messages API

共同基类: LLMProvider (base.py)
共同产出: StreamChunk (content/tool_call/reasoning/finish_reason/error)
```

### 4.2 StreamChunk 数据类

```python
@dataclass
class StreamChunk:
    content: Optional[str]           # 文本
    tool_call: Optional[ToolCall]    # 工具调用
    finish_reason: Optional[str]     # 结束原因
    usage: Optional[dict]            # token 用量
    error: Optional[str]             # 用户友好错误
    raw_error: Optional[str]         # 原始错误 (用于 key rotation 判断)
    reasoning_content: Optional[str] # 思考链
    provider_payload: Optional[dict] # Provider 特定数据

    # 便捷属性: is_content, is_tool_call, is_done, is_error, is_reasoning
```

### 4.3 供应商注册表 (registry.py)

23 个供应商元数据，每项包含: name, provider_type, default_api_base, default_model, requires_api_key, supports_thinking。

23 个中大多数兼容 OpenAI API，只有 Anthropic 需要独立实现。

### 4.4 运行时状态 (runtime.py)

**get_provider_runtime_state(config, provider_id):**
```
返回 ProviderRuntimeState:
  - selectable: 是否可选用 (需要 Key 但未配置 → False)
  - api_key: 当前活跃 Key
  - api_keys: 所有已配置 Key (用于轮换)
  - api_base: API Base URL
  - reason: 不可选原因 (用于友好提示)
```

**KeyRotator 轮换:**
- `mark_key_failed(failed_key)` → 下一个可用 Key
- 按 provider 跟踪轮换状态
- AgentLoop 在 401/429 错误时触发，最多 3 次

---

## 5. Tools 模块

> 路径: `backend/modules/tools/` | 19 个文件 | 工具系统

### 5.1 架构

```
ToolRegistry (registry.py)
  ├─ register(tool) / unregister(name)
  ├─ get_definitions() → OpenAI format function list
  ├─ execute(name, arguments) → 含参数校验+审计日志+对话历史
  ├─ set_session_id() / set_channel() / set_cancel_token()  (contextvars)
  └─ get_stats() / clear()

Tool 基类 (base.py):
  ├─ name, description
  ├─ execute(**kwargs) → str
  ├─ get_definition() → dict
  └─ validate_params(params) → list[str]
```

### 5.2 execute() 执行流程

```
execute(tool_name, arguments)
  ├─ 1. 查找工具 (不存在 → 友好错误)
  ├─ 2. 生成 call_id, 记录审计 start
  ├─ 3. 检测参数解析失败 (LLM JSON 截断) → 友好指导
  ├─ 4. validate_params() 校验
  ├─ 5. 设置执行上下文 (contextvars Token)
  ├─ 6. await tool.execute(**arguments)
  ├─ 7. 更新审计 (success/error + duration_ms)
  ├─ 8. 记录对话历史 (conversation_history)
  └─ 9. 返回结果 (绝不抛异常)
```

### 5.3 17 个内置工具

| 类别 | 工具名 | 文件 | 说明 |
|---|---|---|---|
| 文件 | `read_file` | filesystem.py | 读取文件，支持行号范围 |
| 文件 | `write_file` | filesystem.py | 写入/追加 (mode="write"/"append") |
| 文件 | `edit_file` | filesystem.py | 精确文本替换 (old_string→new_string) |
| 文件 | `list_dir` | filesystem.py | 列出目录 |
| Shell | `exec` | shell.py | 命令执行，含危险模式正则过滤 |
| Web | `web_fetch` | web.py | HTTP 请求/网页抓取 |
| Agent | `spawn` | spawn.py | 创建后台子代理 |
| Agent | `workflow_run` | workflow_tool.py | 多智能体工作流 |
| Agent | `external_coding_agent` | external_coding_agent.py | 调用 Claude Code/Codex |
| 记忆 | `memory` | memory_tool.py | 统一记忆读写搜索 |
| 媒体 | `send_media` | send_media.py | 通过渠道发送文件 |
| 截图 | `screenshot` | screenshot.py | 屏幕截图 |
| 搜索 | `file_search` | file_search.py | 文件名/内容搜索 |
| 知识 | `wiki` | wiki/tool.py | BM25 全文检索 |
| 特殊 | `xiaozhi_message` | xiaozhi_message.py | 小智AI消息 |
| 内部 | `conversation_history` | conversation_history.py | 工具对话历史 |
| 内部 | `monitoring` | monitoring.py | 工具执行监控 |

### 5.4 register_all_tools() 注册顺序 (setup.py)

```
1. 文件系统 (read/write/edit/list_dir)    ← 总是注册
2. Shell (exec)                           ← 总是注册
3. 外部编码代理                            ← 条件: 有 enabled profile
4. Web (web_fetch)                        ← 总是注册
5. spawn                                  ← 条件: subagent_manager 存在
6. workflow_run                           ← 条件: subagent_manager 存在
7. send_media                             ← 条件: channel_manager 存在
8. screenshot                             ← 总是注册
9. file_search                            ← 总是注册
10. memory                                ← 条件: memory_store 存在
11. wiki                                  ← 总是注册
12. xiaozhi_message                       ← 条件: 小智渠道启用
```

ChannelManager 创建后会触发第二次 `register_all_tools()` 调用，将 channel_manager 注入 `send_media` 等工具。

### 5.5 contextvars 异步安全

```python
_session_id_context: ContextVar = ContextVar('session_id')  # 非 self.session_id
_session_id_context.set(value)  # 仅当前协程可见
```

确保同一进程多个并发 WebSocket 连接不互相干扰。

### 5.6 审计日志

**类:** `FileAuditLogger`  
**格式:** JSONL (`audit_YYYY-MM-DD.jsonl`)  
**内容:** call_id, tool_name, arguments, result, status, duration_ms, session_id, timestamp

---

## 6. Channels 模块

> 路径: `backend/modules/channels/` | 14 个文件 | IM 渠道集成

### 6.1 架构

```
ChannelManager (manager.py)
  ├─ 初始化: 遍历 _CHANNEL_REGISTRY → 按配置创建实例
  ├─ 生命周期: start_all() / stop_all()
  ├─ 监督: _start_channel_supervised() (指数退避重连)
  ├─ 入站: _on_inbound_message() → 转发到消息总线
  └─ 出站: _dispatch_outbound() → 路由到对应渠道

BaseChannel (base.py) — 抽象基类:
  ├─ start() / stop() / send() / test_connection()  (抽象)
  ├─ is_allowed(sender_id) — 白名单校验
  └─ _handle_message() → 回调 → ChannelManager
```

### 6.2 8 个渠道实现

| 渠道 | 类 | 特性 |
|---|---|---|
| Telegram | TelegramChannel | 长轮询/Webhook |
| QQ | QQChannel | QQ Bot SDK |
| 微信 | WeChatChannel | ClawBot, 多账号, typing 提示 |
| 钉钉 | DingTalkChannel | 流式卡片 |
| 飞书 | FeishuChannel | WebSocket 长连接 |
| 微博 | WeiboChannel | — |
| 企微 | WeComChannel | 企业微信 Bot |
| 小智 | XiaozhiChannel | AI 对话模式 |

### 6.3 ChannelMessageHandler (handler.py, ~2300 行)

**handle_message() 流程:**

```
1. _normalize_channel_inbound_content()  # 清理 @mention
2. _resolve_session_route()              # 群聊/私聊, 多账号路由
3. 命令分发 (优先级拦截):
     /new /list /all /switch /clear /stop /help
     /team /route /coder /provider /personality
4. 路由决策:
     /route ai     → AI 自主决策 (默认)
     /route direct → 直转 external_coding_agent
5. _get_or_create_session()              # 会话命名: {channel}:{account}:{chat}:{timestamp}
6. _resolve_runtime_config_for_session() # 配置覆盖链
7. _get_session_history()                # 获取历史 (>15条触发摘要)
8. AgentLoop.process_message()           # 流式 / 传统
9. 保存回复 + 发布到出站总线
```

**命令总览:**

| 命令 | 简写 | 功能 |
|---|---|---|
| `/new` | `/n` | 新建会话 |
| `/list` | `/l` | 当前聊天会话列表 |
| `/all` | `/al` | 所有会话列表 |
| `/switch <编号\|ID>` | `/s` | 切换会话 |
| `/clear` | `/c` | 清除历史 |
| `/stop` | — | 停止当前任务 |
| `/help` | `/h` | 帮助 |
| `/provider [编号\|ID] [模型]` | `/m` | 查看/切换模型 |
| `/personality [编号\|ID]` | `/p` | 查看/切换性格 |
| `/team [团队名] [任务]` | — | 执行团队工作流 |
| `/route [ai\|direct\|default]` | `/rt` | 切换路由模式 |
| `/coder [profile\|default]` | `/cdr` | 切换外部编程代理 |

**群聊会话路由策略:**

| 场景 | 会话范围 |
|---|---|
| 私聊 (单机器人) | private_independent: 每人独立 |
| 群聊 (主机器人) | group_shared_primary: 全群共享 |
| 群聊 (非主机器人) | group_independent: 各机器人独立 |
| 群聊镜像 | 非主机器人问答自动镜像到主机器人会话 |

### 6.4 数据模型

```python
InboundMessage:
    channel, sender_id, chat_id, content, media, metadata
    # metadata: account_id, is_group, sender_name, session_id...

OutboundMessage:
    channel, chat_id, content, media, metadata
```

---

## 7. 其他模块

### 7.1 MCP 客户端

> 路径: `backend/modules/mcp/`

**McpClientManager (单例):**
- 支持三种传输: stdio (子进程), SSE, Streamable HTTP
- 自动推断: `config.command` → stdio, `/sse` 结尾 → SSE, 有 URL → streamable_http
- 工具包装为 `MCPToolWrapper`，自动加 `mcp_` 前缀
- 启动时非阻塞后台连接，不阻塞主流程
- `sync_to_registry_sync()` 将 MCP 工具同步到会话级 ToolRegistry

### 7.2 Wiki 知识库

> 路径: `backend/modules/wiki/`

**WikiService:**
- 存储: `workspace/wiki/concepts/*.md` (Markdown + YAML frontmatter)
- 索引: BM25 全文检索，持久化到 `bm25_index.json`
- 增量更新: 对比文件 mtime 自动同步
- LRU 缓存: 搜索结果缓存 (带版本号失效)

**WikiTool 接口:**
```
wiki(action="search", query="...", top_k=5)       # 搜索
wiki(action="get", doc_id="...")                   # 获取
wiki(action="list")                                # 列出
wiki(action="add", title="...", content="...")     # 添加
wiki(action="update", doc_id="...", content="...") # 更新
wiki(action="delete", doc_id="...")                # 删除
```

### 7.3 Cron 定时任务

> 路径: `backend/modules/cron/`

**CronScheduler:**
- 精确按需唤醒: 计算所有活跃任务的下次执行时间，取最近的那个设 timer
- `Semaphore(max_concurrent=3)` 控制并发
- 每个任务独立 `asyncio.Task` 执行，超时保护 (1200s)

**CronExecutor:**
- 创建独立 AgentLoop 实例执行 task.message
- 可选 `deliver_response` (将结果发送到渠道)

**HeartbeatService:**
- 内置 cron 任务
- 检测用户空闲 → 超过阈值 → 主动问候
- 支持安静时段 + 每日上限

### 7.4 Config 配置

> 路径: `backend/modules/config/`

- **Schema:** `AppConfig` — Pydantic v2 完整配置模型
- **Loader:** `ConfigLoader` — 从 SQLite `settings` 表加载 (key-value → JSON)
- **Key 格式:** `config.model.provider`, `config.persona.ai_name` 等
- **默认值:** `AppConfig()` 提供全部 Pydantic 默认值

### 7.5 Session 会话

> 路径: `backend/modules/session/`

- Session CRUD + Message 持久化 (SQLite)
- `resolve_session_runtime_config()` — 配置覆盖链
- 会话级模型/性格覆盖: `session_model_config` / `session_persona_config` JSON 字段

### 7.6 WebSocket

> 路径: `backend/ws/` + `backend/modules/websocket/`

**端点:** `/ws/chat` → `handle_websocket()`
- 认证: 本地直连放行，远程校验 token
- 每个连接独立 ToolRegistry (含 MCP 同步)
- 双向: 前端消息 → AgentLoop → 流式 → WebSocket → 前端

**实时事件:**

| 事件 | 说明 |
|---|---|
| `chunk` | LLM 流式文本块 |
| `reasoning_chunk` | 思考链输出 |
| `tool_execution` | 工具执行通知 |
| `workflow_agent_start/tool_call/chunk/complete` | 工作流进度 |
| `mcp_status` | MCP 连接状态 |

### 7.7 External Agents

> 路径: `backend/modules/external_agents/`

将 Claude Code、Codex、OpenCode 包装为可调用工具:
- **CLI Adapter:** subprocess 调用外部 CLI
- **会话模式:** stateless (每次独立) / native (工具自带) / synthetic (拼接历史)
- **自然语言路由:** "用 codex 帮我修" → 自动解析 profile + task

### 7.8 Frontend 前端

> 路径: `frontend/` | Vue 3 + TypeScript + Vite

**模块 (9 个):**

| 模块 | 功能 |
|---|---|
| chat | 消息列表 + 输入框 + 附件 + Markdown 渲染 |
| settings | 模型/角色/安全/工作空间 + 渠道子配置 |
| tools | 工具列表 + 详情面板 |
| skills | 技能搜索/安装/启用/禁用 |
| mcp | MCP 服务器管理 |
| wiki | 知识库文档管理 |
| memory | 记忆查看与管理 |
| scheduler | 定时任务管理 |
| teams | 多智能体团队配置 |
| system | 系统状态 (日志/队列/任务) |

**10 个 Pinia Store:** chat, settings, tools, memory, skills, cron, channels, agentTeams, externalCodingTools, app

### 7.9 数据模型 (ORM)

> 路径: `backend/models/` | SQLAlchemy + SQLite

| 模型 | 表 | 关键字段 |
|---|---|---|
| Session | sessions | name, channel_context, session_model_config, session_persona_config, use_custom_config |
| Message | messages | session_id, role, content, message_context |
| Personality | personalities | name, description, traits, speaking_style, is_active |
| AgentTeam | agent_teams | name, description, mode, agents(JSON), cross_review, is_active, model_config(JSON) |
| CronJob | cron_jobs | name, schedule, message, channel, account_id, chat_id, enabled |
| Setting | settings | key, value |
| Task | tasks | label, message, session_id, status, progress, result, error, tool_call_records |
| ToolConversation | tool_conversations | session_id, tool_name, arguments, result, error, user_message, message_id |
