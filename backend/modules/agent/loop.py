"""Agent Loop - 核心 Agent 循环处理逻辑"""

import asyncio
import inspect
import json
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from loguru import logger
from backend.modules.tools.conversation_history import get_conversation_history


def _is_key_rotation_eligible_error(error_text: str) -> bool:
    """判断错误是否适合触发 key 轮换重试。"""
    lower = (error_text or "").lower()
    auth_hints = (
        "401", "unauthorized", "invalid api key", "invalid_api_key",
        "authentication", "invalid token", "token is unusable",
        "api key", "apikey", "access denied",
        "insufficient_quota", "account_deactivated",
    )
    rate_hints = (
        "429", "rate limit", "rate_limit", "quota",
        "too many requests", "capacity", "overloaded",
    )
    return any(hint in lower for hint in auth_hints + rate_hints)


class AgentLoop:
    """Agent 主循环类 - 处理消息、调用 LLM、执行工具、生成响应"""

    MAX_KEY_ROTATION_RETRIES = 3

    def __init__(
        self,
        provider,
        workspace: Path,
        tools,
        context_builder=None,
        session_manager=None,
        subagent_manager=None,
        model: Optional[str] = None,
        max_iterations: int = 25,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        thinking_enabled: bool = True,
    ):
        """初始化 Agent 循环实例。

        Args:
            provider: LLM provider 实例，负责与具体大模型 API 通信
                （如 OpenAI、Anthropic 等）。
            workspace: 工作区根目录路径，作为工具执行的基准目录。
            tools: 工具注册表（ToolRegistry）实例，管理所有可用工具的
                定义与调度。
            context_builder: 上下文构建器，负责将对话历史、系统提示、
                当前消息等组装成 LLM 可接收的 messages 格式。为 None 时
                使用默认的消息拼接逻辑。
            session_manager: 会话管理器，用于持久化对话历史。
            subagent_manager: 子代理管理器，用于派生与管理子代理。
            model: 指定使用的模型名称，None 表示使用 provider 的默认模型。
            max_iterations: 单次请求的最大 Agent 循环迭代次数，默认 25。
            max_retries: 工具调用失败时的最大重试次数，默认 3。
            retry_delay: 工具调用重试之间的等待秒数，默认 1.0。
            temperature: LLM 采样温度（0.0 = 确定性输出），默认 0.0。
            max_tokens: LLM 单次响应的最大 token 数，默认 4096。
            thinking_enabled: 是否启用模型的思考/推理链（thinking）功能，
                默认 True。
        """
        self.provider = provider
        self.workspace = workspace
        self.tools = tools
        self.context_builder = context_builder
        self.session_manager = session_manager
        self.subagent_manager = subagent_manager
        self.model = model
        self.max_iterations = max_iterations
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.thinking_enabled = thinking_enabled
        self._key_rotation_count = 0
        
        logger.debug(
            f"AgentLoop initialized: max_iterations={max_iterations}, max_retries={max_retries}"
        )

    @staticmethod
    def _summarize_tool_calls_for_log(tool_calls: List[Any]) -> str:
        """将工具调用列表格式化为可读的日志摘要字符串。

        用于在日志中快速识别当前批次包含的工具调用及其 ID，
        避免在日志中打印完整的工具参数（可能过长或包含敏感信息）。

        Args:
            tool_calls: 工具调用对象列表，每个对象应包含 id 和 name 属性。

        Returns:
            形如 "tool_name#tool_id, tool_name#tool_id" 的摘要字符串；
            列表为空时返回 "<none>"。
        """
        parts = []
        for tool_call in tool_calls:
            tool_id = str(getattr(tool_call, "id", "") or "").strip() or "<empty>"
            tool_name = str(getattr(tool_call, "name", "") or "").strip() or "<unknown>"
            parts.append(f"{tool_name}#{tool_id}")
        return ", ".join(parts) if parts else "<none>"

    def _resolve_execution_runtime(
        self,
        model_override: Optional[Dict[str, Any]] = None,
    ) -> tuple[Any, Optional[str], float, int, int, bool]:
        """解析当前消息执行应使用的 provider 和模型参数。

        优先使用 model_override 中指定的配置覆盖默认值。如果
        model_override 指定了独立的 provider、api_key 或 api_base，
        则会动态创建一个新的 provider 实例；创建失败时回退到基础
        provider。

        Args:
            model_override: 可选的模型配置覆盖字典，可包含以下键：
                - model: 模型名称
                - temperature: 采样温度
                - max_tokens: 最大 token 数
                - max_iterations: 最大迭代次数
                - thinking_enabled: 是否启用思考链
                - api_mode: API 模式（如 "chat_completions"）
                - provider: 覆盖的 provider ID
                - api_key: 覆盖的 API Key
                - api_base: 覆盖的 API Base URL

        Returns:
            tuple[provider, model, temperature, max_tokens,
                  max_iterations, thinking_enabled]:
            解析后的 (provider实例, 模型名, 温度, 最大token数,
                       最大迭代次数, 是否启用思考链)。
        """
        base_provider = self.provider
        base_model = self.model
        base_temperature = self.temperature
        base_max_tokens = self.max_tokens
        base_max_iterations = self.max_iterations
        base_thinking_enabled = self.thinking_enabled
        base_api_mode = getattr(base_provider, "api_mode", "chat_completions")

        if not model_override:
            return (
                base_provider,
                base_model,
                base_temperature,
                base_max_tokens,
                base_max_iterations,
                base_thinking_enabled,
            )

        candidate_provider = base_provider
        candidate_model = model_override.get("model", base_model)
        candidate_temperature = model_override.get("temperature", base_temperature)
        candidate_max_tokens = model_override.get("max_tokens", base_max_tokens)
        candidate_max_iterations = model_override.get(
            "max_iterations",
            base_max_iterations,
        )
        candidate_api_mode = model_override.get("api_mode", base_api_mode)
        candidate_thinking_enabled = model_override.get(
            "thinking_enabled",
            base_thinking_enabled,
        )

        override_provider = model_override.get("provider")
        override_api_key = model_override.get("api_key") or None
        override_api_base = model_override.get("api_base") or None

        if override_provider or override_api_key or override_api_base:
            try:
                from backend.modules.providers import create_provider
                from backend.modules.config.loader import config_loader
                from backend.modules.providers.runtime import (
                    build_provider_unavailable_message,
                    get_provider_runtime_state,
                )

                provider_id = override_provider or config_loader.config.model.provider
                runtime_state = get_provider_runtime_state(
                    config_loader.config,
                    provider_id,
                    api_key_override=override_api_key,
                    api_base_override=override_api_base,
                )
                if not runtime_state.selectable:
                    raise ValueError(
                        build_provider_unavailable_message(
                            provider_id,
                            runtime_state.reason,
                        )
                    )

                candidate_provider = create_provider(
                    api_key=runtime_state.api_key or None,
                    api_keys=runtime_state.api_keys or None,
                    api_base=runtime_state.api_base,
                    default_model=candidate_model,
                    api_mode=candidate_api_mode,
                    timeout=getattr(self.provider, "timeout", 120.0),
                    max_retries=getattr(self.provider, "max_retries", self.max_retries),
                    provider_id=provider_id,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to create runtime provider override, falling back to base runtime config: "
                    f"{exc}"
                )
                return (
                    base_provider,
                    base_model,
                    base_temperature,
                    base_max_tokens,
                    base_max_iterations,
                    base_thinking_enabled,
                )

        return (
            candidate_provider,
            candidate_model,
            candidate_temperature,
            candidate_max_tokens,
            candidate_max_iterations,
            candidate_thinking_enabled,
        )

    async def process_message(
        self,
        message: str,
        session_id: str,
        context: Optional[List[Dict[str, Any]]] = None,
        session_summary: Optional[str] = None,
        media: Optional[List[str]] = None,
        channel: Optional[str] = None,
        chat_id: Optional[str] = None,
        account_id: Optional[str] = None,
        cancel_token=None,
        yield_intermediate: bool = True,
        model_override: Optional[Dict[str, Any]] = None,
        persona_override=None,
        tool_event_handler=None,
        reasoning_event_handler=None,
        prefer_direct_workflow_result: bool = False,
    ) -> AsyncIterator[str]:
        """处理用户消息并生成流式响应（async generator）。

        核心 Agent 循环：
        1. 构建 messages 上下文（通过 context_builder 或默认方式）
        2. 解析运行时 provider 与模型参数
        3. 进入迭代循环：调用 LLM → 解析响应 → 执行工具 → 回传结果
        4. 在达到 max_iterations 或 LLM 返回纯文本后退出循环
        5. 将最终响应保存到 session_manager 并写入审计日志

        Args:
            message: 用户输入的原始消息文本。
            session_id: 当前会话的唯一标识符。
            context: 对话历史列表，每条消息为 {"role": ..., "content": ...}
                格式。为 None 时使用空历史。
            session_summary: 历史对话摘要文本，用于超长上下文压缩后
                注入系统提示。
            media: 媒体文件路径列表（图片、音频等），传给 context_builder
                处理。
            channel: 消息来源渠道标识（如 "discord"、"telegram"、"cli"）。
            chat_id: 来源聊天/群组 ID，用于多机器人账号路由。
            account_id: 当前机器人账号 ID，用于多账号渠道。
            cancel_token: 取消令牌对象，包含 is_cancelled 属性。当外部
                设置取消标志时，循环会在下次检查点优雅终止。
            yield_intermediate: 是否实时 yield 流式文本块。True 时每收到
                一个 LLM 文本 chunk 立即产出；False 时只在循环结束时
                一次性产出最终内容。
            model_override: 运行时模型配置覆盖字典，详见
                _resolve_execution_runtime。
            persona_override: 人设/角色覆盖配置，传递给 context_builder
                用于动态切换系统提示。
            tool_event_handler: 工具事件回调函数，接收 ("tool_call"|"tool_result"|"tool_error", data)
                事件，用于前端实时展示工具执行状态。
            reasoning_event_handler: 推理过程事件回调函数，接收 LLM 返回的
                思考链（reasoning）文本块，用于展示模型的内部推理过程。
            prefer_direct_workflow_result: 当工具名为 "workflow_run" 时，
                是否直接将其执行结果作为最终响应返回（跳过后续 LLM 总结）。

        Yields:
            str: 流式产出的 LLM 响应文本块，以及达到限制时的警告信息。
        """
        logger.debug(f"Processing message for session {session_id}")
        
        # 设置工具注册表的会话ID（用于审计日志）和渠道信息
        if self.tools:
            self.tools.set_session_id(session_id)
            self.tools.set_channel(channel)
            # 将取消令牌传递给支持中断的工具（如 WorkflowTool）
            if cancel_token and hasattr(self.tools, 'set_cancel_token'):
                self.tools.set_cancel_token(cancel_token)

            spawn_tool = self.tools.get_tool("spawn")
            if spawn_tool and hasattr(spawn_tool, 'set_context'):
                spawn_tool.set_context(session_id)
        
        if self.context_builder and context is not None:
            messages = self.context_builder.build_messages(
                history=context,
                current_message=message,
                session_summary=session_summary,
                media=media,
                channel=channel,
                chat_id=chat_id,
                account_id=account_id,
                persona_config=persona_override,
            )
        else:
            if context is None:
                context = []
            
            messages = list(context)
            messages.append({
                "role": "user",
                "content": message,
            })

        (
            active_provider,
            runtime_model,
            runtime_temperature,
            runtime_max_tokens,
            runtime_max_iterations,
            runtime_thinking_enabled,
        ) = self._resolve_execution_runtime(model_override)
        
        iteration = 0
        total_tool_calls = 0
        final_content = ""
        direct_result_selected = False
        tool_call_limit_reached = False
        self._key_rotation_count = 0
        request_trace_id = f"{session_id[:8]}-{uuid.uuid4().hex[:8]}"

        logger.info(
            "Agent 循环请求开始: "
            f"trace={request_trace_id}, session={session_id}, model={runtime_model or '<default>'}"
        )

        try:
            while iteration < runtime_max_iterations:
                iteration += 1
                
                if cancel_token and cancel_token.is_cancelled:
                    logger.debug(f"Agent loop cancelled at iteration {iteration}")
                    return
                
                logger.debug(f"Iteration {iteration}: {total_tool_calls} tool calls")
                
                tool_definitions = self.tools.get_definitions() if self.tools else []
                
                content_buffer = ""
                tool_calls_buffer = []
                finish_reason = None
                reasoning_buffer = ""
                provider_payload = None
                provider_trace_kwargs: Dict[str, Any] = {}
                if active_provider.__class__.__module__.endswith(".openai_provider"):
                    provider_trace_kwargs["request_trace_id"] = request_trace_id
                
                async for chunk in active_provider.chat_stream(
                    messages=messages,
                    tools=tool_definitions,
                    model=runtime_model,
                    temperature=runtime_temperature,
                    max_tokens=runtime_max_tokens,
                    thinking_enabled=runtime_thinking_enabled,
                    **provider_trace_kwargs,
                ):
                    if chunk.is_content and chunk.content:
                        content_buffer += chunk.content
                        if yield_intermediate:
                            yield chunk.content
                    
                    if chunk.is_tool_call and chunk.tool_call:
                        tool_calls_buffer.append(chunk.tool_call)
                    
                    if chunk.is_reasoning and chunk.reasoning_content:
                        reasoning_buffer += chunk.reasoning_content
                        if reasoning_event_handler:
                            try:
                                maybe_result = reasoning_event_handler(
                                    chunk.reasoning_content
                                )
                                if inspect.isawaitable(maybe_result):
                                    await maybe_result
                            except Exception as exc:
                                logger.warning(
                                    f"Failed to emit reasoning chunk for session {session_id}: {exc}"
                                )
                    
                    if chunk.has_provider_payload and chunk.provider_payload:
                        provider_payload = chunk.provider_payload

                    if chunk.is_done and chunk.finish_reason:
                        finish_reason = chunk.finish_reason
                    
                    if chunk.is_error:
                        error_text = chunk.raw_error or chunk.error or ""
                        rotated_provider = self._try_key_rotation(
                            active_provider, error_text
                        )
                        if rotated_provider is not None:
                            active_provider = rotated_provider
                            iteration -= 1
                            self._key_rotation_count += 1
                            await asyncio.sleep(1.0)
                            continue
                        yield chunk.error
                        return
                
                if content_buffer:
                    final_content = content_buffer
                elif reasoning_buffer and not tool_calls_buffer:
                    final_content = reasoning_buffer
                
                if tool_calls_buffer:
                    deduped_tool_calls = []
                    seen_tool_call_ids = set()
                    for tc in tool_calls_buffer:
                        if tc.id and tc.id in seen_tool_call_ids:
                            logger.warning(
                                f"Skipping duplicate tool call by id in main loop: {tc.name} ({tc.id})"
                            )
                            continue

                        if tc.id:
                            seen_tool_call_ids.add(tc.id)
                        deduped_tool_calls.append(tc)

                    tool_calls_buffer = deduped_tool_calls

                    logger.info(
                        "已接收工具批次: "
                        f"trace={request_trace_id}, iteration={iteration}, count={len(tool_calls_buffer)}, "
                        f"calls=[{self._summarize_tool_calls_for_log(tool_calls_buffer)}]"
                    )

                    remaining_tool_slots = runtime_max_iterations - total_tool_calls
                    if remaining_tool_slots <= 0:
                        logger.warning(
                            "Reached max tool call limit before executing a new tool call batch; "
                            "aborting batch to avoid sending unmatched tool results upstream"
                        )
                        tool_call_limit_reached = True
                        break
                    if len(tool_calls_buffer) > remaining_tool_slots:
                        logger.warning(
                            f"Truncating tool call batch from {len(tool_calls_buffer)} to "
                            f"{remaining_tool_slots} to keep tool_calls/tool_results aligned"
                        )
                        tool_calls_buffer = tool_calls_buffer[:remaining_tool_slots]

                    tool_call_dicts = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in tool_calls_buffer
                    ]
                    
                    if self.context_builder:
                        messages = self.context_builder.add_assistant_message(
                            messages,
                            content_buffer or None,
                            tool_call_dicts,
                            reasoning_content=reasoning_buffer or None,
                            provider_payload=provider_payload,
                        )
                    else:
                        msg = {
                            "role": "assistant",
                            "content": content_buffer or "",
                            "tool_calls": tool_call_dicts,
                        }
                        if reasoning_buffer:
                            msg["reasoning_content"] = reasoning_buffer
                        if provider_payload:
                            msg.update(provider_payload)
                        messages.append(msg)
                    
                    for tool_call in tool_calls_buffer:
                        if total_tool_calls >= runtime_max_iterations:
                            logger.warning(
                                f"Reached max tool calls limit ({runtime_max_iterations}), "
                                f"skipping remaining tool calls in this iteration"
                            )
                            break
                        
                        if cancel_token and cancel_token.is_cancelled:
                            logger.debug(f"Agent loop cancelled before tool execution")
                            return

                        total_tool_calls += 1
                        tool_name = tool_call.name
                        tool_args = tool_call.arguments
                        tool_id = tool_call.id

                        logger.info(
                            "开始执行工具: "
                            f"trace={request_trace_id}, seq={total_tool_calls}, "
                            f"name={tool_name}, tool_call_id={tool_id}"
                        )

                        if tool_event_handler:
                            try:
                                maybe_result = tool_event_handler(
                                    "tool_call",
                                    {
                                        "tool_name": tool_name,
                                        "arguments": tool_args,
                                        "session_id": session_id,
                                    },
                                )
                                if inspect.isawaitable(maybe_result):
                                    await maybe_result
                            except Exception as e:
                                logger.warning(f"Tool event handler failed before execution: {e}")
                        
                        try:
                            from backend.ws.tool_notifications import notify_tool_execution
                            await notify_tool_execution(
                                session_id=session_id,
                                tool_name=tool_name,
                                arguments=tool_args,
                            )
                        except Exception as e:
                            logger.warning(f"Failed to send tool notification: {e}")
                        
                        start_time = time.time()
                        result = None
                        last_error = None
                        
                        if self.tools:
                            self.tools.set_tool_event_handler(tool_event_handler)
                        try:
                            for attempt in range(self.max_retries):
                                try:
                                    result = await self.execute_tool(tool_name, tool_args)
                                    logger.debug(f"Tool {tool_name} succeeded")
                                    break
                                except Exception as e:
                                    last_error = e
                                    logger.warning(
                                        f"Tool {tool_name} failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                                    )
                                    if attempt < self.max_retries - 1:
                                        await asyncio.sleep(self.retry_delay)
                        finally:
                            if self.tools:
                                self.tools.set_tool_event_handler(None)
                        
                        duration_ms = int((time.time() - start_time) * 1000)
                        
                        if result is not None:
                            try:
                                conversation_history = get_conversation_history()
                                conversation_history.add_conversation(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user_message=message,
                                    result=result,
                                    duration_ms=duration_ms
                                )
                            except Exception as e:
                                logger.warning(f"Failed to record tool conversation: {e}")
                            
                            try:
                                from backend.ws.tool_notifications import notify_tool_execution
                                await notify_tool_execution(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    result=result,
                                )
                            except Exception as e:
                                logger.warning(f"Failed to send tool result notification: {e}")

                            if tool_event_handler:
                                try:
                                    maybe_result = tool_event_handler(
                                        "tool_result",
                                        {
                                            "tool_name": tool_name,
                                            "arguments": tool_args,
                                            "result": result,
                                            "session_id": session_id,
                                            "duration_ms": duration_ms,
                                        },
                                    )
                                    if inspect.isawaitable(maybe_result):
                                        await maybe_result
                                except Exception as e:
                                    logger.warning(f"Tool event handler failed after execution: {e}")

                            if tool_name == "workflow_run" and prefer_direct_workflow_result:
                                final_content = result
                                direct_result_selected = True
                                if result:
                                    yield result
                                break
                            
                            if self.context_builder:
                                messages = self.context_builder.add_tool_result(
                                    messages,
                                    tool_id,
                                    tool_name,
                                    result,
                                )
                            else:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": result,
                                })
                            logger.info(
                                "已追加工具结果: "
                                f"trace={request_trace_id}, name={tool_name}, tool_call_id={tool_id}, "
                                f"status=success, duration_ms={duration_ms}"
                            )
                        else:
                            error_msg = f"Tool execution failed after {self.max_retries} attempts: {str(last_error)}"
                            logger.error(f"Tool {tool_name} failed permanently: {error_msg}")
                            
                            try:
                                conversation_history = get_conversation_history()
                                conversation_history.add_conversation(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    user_message=message,
                                    error=error_msg,
                                    duration_ms=duration_ms
                                )
                            except Exception as e:
                                logger.warning(f"Failed to record tool conversation: {e}")
                            
                            try:
                                from backend.ws.tool_notifications import notify_tool_execution
                                await notify_tool_execution(
                                    session_id=session_id,
                                    tool_name=tool_name,
                                    arguments=tool_args,
                                    error=error_msg,
                                )
                            except Exception as e:
                                logger.warning(f"Failed to send tool error notification: {e}")

                            if tool_event_handler:
                                try:
                                    maybe_result = tool_event_handler(
                                        "tool_error",
                                        {
                                            "tool_name": tool_name,
                                            "arguments": tool_args,
                                            "error": error_msg,
                                            "session_id": session_id,
                                            "duration_ms": duration_ms,
                                        },
                                    )
                                    if inspect.isawaitable(maybe_result):
                                        await maybe_result
                                except Exception as e:
                                    logger.warning(f"Tool event handler failed on error: {e}")
                            
                            if self.context_builder:
                                messages = self.context_builder.add_tool_result(
                                    messages,
                                    tool_id,
                                    tool_name,
                                    error_msg,
                                )
                            else:
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": tool_name,
                                    "content": error_msg,
                                })
                            logger.info(
                                "已追加工具结果: "
                                f"trace={request_trace_id}, name={tool_name}, tool_call_id={tool_id}, "
                                f"status=error, duration_ms={duration_ms}"
                            )
                    if direct_result_selected:
                        break
                else:
                    if not yield_intermediate and content_buffer:
                        yield content_buffer
                    break

                if direct_result_selected:
                    break
            
            # 检查是否达到限制
            if (
                tool_call_limit_reached
                or iteration >= runtime_max_iterations
                or total_tool_calls >= runtime_max_iterations
            ):
                if tool_call_limit_reached or total_tool_calls >= runtime_max_iterations:
                    logger.warning(f"Max tool calls ({runtime_max_iterations}) reached")
                    warning_msg = f"\n\n[达到最大工具调用次数 {runtime_max_iterations}]"
                else:
                    logger.warning(f"Max iterations ({runtime_max_iterations}) reached")
                    warning_msg = f"\n\n[达到最大迭代次数 {runtime_max_iterations}]"
                yield warning_msg
                final_content += warning_msg
            
            # 保存到会话（如果有 session_manager）
            if self.session_manager and final_content:
                try:
                    session = self.session_manager.get_or_create(session_id)
                    session.add_message("user", message)
                    session.add_message("assistant", final_content)
                    self.session_manager.save(session)
                except Exception as e:
                    logger.warning(f"Failed to save session: {e}")
            
            # 记录AI完整响应到审计日志
            if self.tools and final_content:
                try:
                    from backend.modules.tools.file_audit_logger import file_audit_logger
                    file_audit_logger.record_ai_response(
                        session_id=session_id,
                        user_message=message,
                        ai_response=final_content,
                        duration_ms=None  # 暂时不记录耗时
                    )
                except Exception as e:
                    logger.warning(f"Failed to record AI response to audit log: {e}")
                
        except Exception as e:
            logger.exception(f"Error in agent loop: {e}")
            raise

    def _try_key_rotation(
        self,
        current_provider: Any,
        error_text: str,
    ) -> Optional[Any]:
        """尝试通过 Key 轮换从 API 错误中恢复。

        当 LLM API 返回认证或限流相关错误时，切换到备用 API Key
        并重新创建 provider 实例，以绕过当前 Key 的配额或封禁限制。

        触发条件：
        - 错误信息包含认证相关关键词（401、unauthorized 等）
        - 错误信息包含限流相关关键词（429、rate limit 等）
        - 轮换次数未超过 MAX_KEY_ROTATION_RETRIES
        - provider 配置了多个 API Key

        Args:
            current_provider: 当前使用的 provider 实例。
            error_text: LLM API 返回的错误文本，用于判断是否适合触发
                轮换。

        Returns:
            轮换成功时返回新的 provider 实例；无法轮换或轮换耗尽时
            返回 None。
        """
        if not _is_key_rotation_eligible_error(error_text):
            return None

        if self._key_rotation_count >= self.MAX_KEY_ROTATION_RETRIES:
            logger.warning(
                f"Key rotation limit reached ({self.MAX_KEY_ROTATION_RETRIES}), "
                f"stopping rotation attempts"
            )
            return None

        provider_id = getattr(current_provider, "provider_id", None)
        if not provider_id:
            return None

        from backend.modules.providers.runtime import get_key_rotator, KeyRotator
        from backend.modules.config.loader import config_loader
        from backend.modules.providers.runtime import get_provider_runtime_state

        config = config_loader.config
        runtime_state = get_provider_runtime_state(config, provider_id)
        api_keys = runtime_state.api_keys

        if len(api_keys) <= 1:
            logger.debug(
                f"Key rotation skipped for {provider_id}: only {len(api_keys)} key(s) available"
            )
            return None

        rotator = get_key_rotator(provider_id, api_keys)
        current_key = getattr(current_provider, "api_key", "") or ""
        next_key = rotator.mark_key_failed(current_key)

        if not next_key or next_key == current_key:
            logger.warning(
                f"Key rotation exhausted for {provider_id}: no alternative key available"
            )
            return None

        logger.info(
            f"Key rotation for {provider_id}: switching from "
            f"{current_key[:8]}... to {next_key[:8]}... "
            f"(error: {error_text[:100]})"
        )

        from backend.modules.providers import create_provider

        try:
            new_provider = create_provider(
                api_key=next_key,
                api_keys=api_keys,
                api_base=runtime_state.api_base,
                default_model=getattr(current_provider, "default_model", None),
                api_mode=getattr(current_provider, "api_mode", "chat_completions"),
                timeout=getattr(current_provider, "timeout", 120.0),
                max_retries=getattr(current_provider, "max_retries", self.max_retries),
                provider_id=provider_id,
            )
            return new_provider
        except Exception as exc:
            logger.warning(f"Failed to create rotated provider: {exc}")
            return None

    async def execute_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """
        执行工具调用
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            
        Returns:
            str: 工具执行结果
            
        Raises:
            ValueError: 工具不存在
            Exception: 工具执行失败
        """
        if not self.tools:
            raise ValueError("ToolRegistry not initialized")
        
        logger.debug(f"执行工具: {tool_name}")
        
        try:
            result = await self.tools.execute(tool_name, arguments, auto_record=False)
            return result
            
        except Exception as e:
            logger.error(f"Tool execution failed: {tool_name} - {e}")
            raise

    async def process_direct(
        self,
        content: str,
        session_id: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        account_id: Optional[str] = None,
    ) -> str:
        """
        直接处理消息（用于 CLI 或 cron 使用）
        
        Args:
            content: 消息内容
            session_id: 会话标识符
            channel: 来源渠道（用于上下文）
            chat_id: 来源聊天 ID（用于上下文）
            account_id: 当前机器人账号 ID（多机器人渠道）
        
        Returns:
            Agent 的响应
        """
        response_parts = []
        
        # 传入空的 context 列表
        async for chunk in self.process_message(
            message=content,
            session_id=session_id,
            context=[],  
            channel=channel,
            chat_id=chat_id,
            account_id=account_id,
        ):
            response_parts.append(chunk)
        
        return "".join(response_parts)
