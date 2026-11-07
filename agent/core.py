from __future__ import annotations

# =============================================================================
# ReAct agent loop
# =============================================================================
# The loop uses the OpenAI-compatible Chat Completions "tool calling" format.
# A ReAct step is:
#   1. The model decides whether it needs a tool.
#   2. If needed, it emits one or more tool calls.
#   3. The tool results are appended to the message list.
#   4. The model receives the updated context and continues.
#   5. When it produces ordinary assistant text, the loop returns that answer.
#
# Conversation state is stored in self.messages, which is a normal Chat
# Completions message list. This makes the loop easy to inspect, persist, and
# later stream over WebSocket.
# =============================================================================

import argparse
import difflib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Support both:
#   python -m agent.core
#   python agent/core.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.customization import AgentsInstructions, load_agents_instructions
from agent.hooks import HookManager
from agent.mcp_client import MCPClientManager
from agent.reviewer import ReviewerAgent, reviewer_enabled
from agent.skills import SkillManager
from agent.tools import OPENAI_TOOLS, TOOL_FUNCTIONS, result_to_text


SYSTEM_PROMPT = """You are a careful desktop coding agent.

Use a ReAct-style tool loop internally:
- Understand the user's request.
- If you need file contents, command output, or a filesystem change, call the
  appropriate tool instead of guessing.
- Inspect every tool result and decide whether another action is needed.
- Never invent file contents, command output, or successful edits.
- When enough evidence is available, answer the user directly and concisely.

Tool-use rules:
- Read a file before editing it unless the user has already supplied the exact
  content and position.
- edit() requires a unique exact old string. If it is ambiguous, read more
  context and use a longer old string. Never guess a replacement location.
- Prefer small, reversible shell commands. Do not run destructive commands
  unless the user explicitly requests them.
- Relative paths are resolved from the current working directory.
- Answer in the same language as the user.

Do not reveal hidden chain-of-thought. You may provide a short summary of the
actions taken and the final result.
"""


DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
MAX_TOOL_MESSAGE_CHARS = 60_000


class AgentInterrupted(RuntimeError):
    """Raised when a client requests cancellation at a safe loop boundary."""


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AgentInterrupted("Agent run interrupted by client")


def _emit_event(
    on_event: Callable[[dict[str, Any]], None] | None,
    event_type: str,
    message: str | None = None,
    **payload: Any,
) -> None:
    """Send one high-level progress event to the caller.

    This is intentionally a progress/observability stream, not a disclosure of
    the model's hidden chain-of-thought. Consumers receive status, tool actions,
    tool results and the final answer.
    """

    if on_event is None:
        return

    event: dict[str, Any] = {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    if message is not None:
        event["message"] = message

    try:
        on_event(event)
    except Exception as exc:
        # Observability must not break the agent loop. A disconnected websocket,
        # for example, should be handled by the server rather than corrupting
        # the conversation state.
        print(f"[event callback error] {type(exc).__name__}: {exc}", file=sys.stderr)


def _describe_tool_call(name: str, arguments: dict[str, Any]) -> str:
    if name == "use_skill":
        return f"加载技能：{arguments.get('name', '?')}"
    if name == "review_changes":
        return "启动隔离 reviewer"
    if name.startswith("mcp__"):
        return f"调用 MCP 工具：{name}"
    if name == "read":
        return f"读取文件：{arguments.get('path', '?')}"
    if name == "write":
        return f"写入文件：{arguments.get('path', '?')}"
    if name == "edit":
        return f"编辑文件：{arguments.get('path', '?')}"
    if name == "bash":
        return f"执行命令：{arguments.get('command', '?')}"
    return f"调用工具：{name}"


def _snapshot_file_for_diff(
    name: str,
    arguments: dict[str, Any],
) -> tuple[Path, str] | None:
    """Capture a file before write/edit so the UI can receive a unified diff."""

    if name not in {"write", "edit"}:
        return None
    raw_path = arguments.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    try:
        before = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        before = ""
    return path, before


def _build_file_diff(snapshot: tuple[Path, str] | None) -> str | None:
    """Build a bounded unified diff for a write/edit tool result."""

    if snapshot is None:
        return None
    path, before = snapshot
    try:
        after = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, IsADirectoryError, OSError):
        after = ""
    if before == after:
        return None

    diff = "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path.name}",
            tofile=f"b/{path.name}",
            lineterm="",
        )
    )
    if len(diff) > 20_000:
        diff = diff[:20_000] + "\n... [diff truncated]"
    return diff


def _load_dotenv_if_available() -> None:
    """Load .env when python-dotenv is installed.

    The desktop app will eventually own configuration management, but loading
    .env here makes `python -m agent.core` convenient during development.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _create_openai_client() -> Any:
    """Create an OpenAI-compatible client from environment variables."""

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The openai package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    # Local OpenAI-compatible servers often do not require a real key. Allowing
    # a placeholder keeps local development friction low.
    if not api_key:
        if base_url:
            api_key = "not-needed"
        else:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env or pass a custom client."
            )

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _message_text(content: Any) -> str:
    """Normalize assistant content to plain text.

    Chat Completions usually returns a string. Some compatible providers may
    return a list of content parts, so we handle both without tying the loop to
    one provider's exact response classes.
    """

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content)


def _tool_call_dict(tool_call: Any) -> dict[str, Any]:
    """Convert an SDK tool-call object into a message-list dictionary."""

    if isinstance(tool_call, dict):
        return tool_call

    if hasattr(tool_call, "model_dump"):
        data = tool_call.model_dump(exclude_none=True)
        if isinstance(data, dict):
            return data

    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": getattr(function, "arguments", "{}"),
        },
    }


def _clip_tool_message(text: str) -> str:
    """Bound one tool result before it enters the model context."""

    if len(text) <= MAX_TOOL_MESSAGE_CHARS:
        return text
    omitted = len(text) - MAX_TOOL_MESSAGE_CHARS
    return (
        text[:MAX_TOOL_MESSAGE_CHARS]
        + f"\n\n[Tool output truncated: {omitted} characters omitted]"
    )


def _summarize_tool_result(result: Any) -> Any:
    """Remove inline image payloads from UI traces and text-only tool messages."""

    if isinstance(result, dict) and result.get("kind") == "image":
        return {
            key: value
            for key, value in result.items()
            if key != "data_url"
        }
    return result


def _image_user_message(result: Any) -> dict[str, Any] | None:
    """Create a multimodal user message for an image returned by read()."""

    if not isinstance(result, dict) or result.get("kind") != "image":
        return None

    data_url = result.get("data_url")
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        return None

    path = str(result.get("path", "image"))
    mime_type = str(result.get("mime_type", "image"))
    size_bytes = int(result.get("size_bytes", 0))
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": (
                    f"The read tool loaded image: {path} "
                    f"({mime_type}, {size_bytes} bytes). "
                    "Inspect the attached image when answering."
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": data_url},
            },
        ],
    }


class ReActAgent:
    """A minimal multi-step ReAct agent using Chat Completions tools.

    Args:
        max_steps: Maximum model turns for one user request. The final turn is
            made without tools so the model is encouraged to produce an answer.
        client: Optional pre-built OpenAI-compatible client. Injecting a client
            is useful for tests or for plugging in a different provider.
        model: Model name. Defaults to OPENAI_MODEL or DEFAULT_MODEL.
        verbose: Print tool actions to stderr for terminal debugging.
    """

    def __init__(
        self,
        max_steps: int = 6,
        *,
        client: Any | None = None,
        model: str | None = None,
        verbose: bool = True,
        skills: SkillManager | None = None,
        mcp: MCPClientManager | None = None,
        agents: AgentsInstructions | None = None,
        hooks: HookManager | None = None,
        reviewer: ReviewerAgent | None = None,
    ) -> None:
        try:
            max_steps = int(max_steps)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_steps must be an integer") from exc
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")

        backend = os.getenv("AGENT_MODEL_BACKEND", "auto").strip().lower()
        if backend not in {"auto", "openai", "mock"}:
            raise ValueError(
                "AGENT_MODEL_BACKEND must be 'auto', 'openai', or 'mock'"
            )
        if backend == "auto":
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            base_url = os.getenv("OPENAI_BASE_URL", "").strip().rstrip("/")
            has_custom_base_url = bool(
                base_url and base_url != DEFAULT_OPENAI_BASE_URL
            )
            backend = "openai" if api_key or has_custom_base_url else "mock"

        self.max_steps = max_steps
        self.model = model or (
            "mock" if backend == "mock" else os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        )
        self.verbose = verbose
        if client is not None:
            self.client = client
        elif backend == "mock":
            from agent.mock_client import StableMockClient

            self.client = StableMockClient()
        else:
            self.client = _create_openai_client()

        self.skills = skills if skills is not None else SkillManager()
        self.mcp = mcp if mcp is not None else MCPClientManager()
        self.agents = agents if agents is not None else load_agents_instructions()
        self.hooks = hooks if hooks is not None else HookManager()
        self.tools: list[dict[str, Any]] = list(OPENAI_TOOLS)
        self.tool_functions: dict[str, Callable[..., Any]] = dict(TOOL_FUNCTIONS)
        self._mcp_loaded = False

        self.system_prompt = SYSTEM_PROMPT
        if self.agents.prompt:
            self.system_prompt += "\n\n" + self.agents.prompt

        catalog = self.skills.catalog_prompt()
        if catalog:
            self.system_prompt += (
                "\n\n" + catalog + "\n"
                "The catalog is not the skill body. Call use_skill only when needed."
            )
            self.tools.append(self.skills.use_skill_tool())
            self.tool_functions["use_skill"] = self._use_skill

        self.reviewer: ReviewerAgent | None = None
        if reviewer_enabled():
            if reviewer is not None:
                self.reviewer = reviewer
            else:
                if backend == "mock":
                    from agent.mock_client import StableMockClient

                    reviewer_client = StableMockClient()
                else:
                    reviewer_client = _create_openai_client()
                self.reviewer = ReviewerAgent(
                    workspace=Path.cwd(),
                    model=self.model,
                    client=reviewer_client,
                )
            self.tools.append(self.reviewer.review_tool())
            self.tool_functions["review_changes"] = self._review_changes

        # The full conversation context lives here. Do not replace this with a
        # single prompt string: tool calls and tool results must remain in order
        # for subsequent model requests to be valid.
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt}
        ]
        self.trace: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history while keeping configuration."""

        self.messages = [{"role": "system", "content": self.system_prompt}]
        self.trace = []

    def _use_skill(self, name: str) -> str:
        skill = self.skills.load(name)
        return (
            f"Skill: {skill.entry.name}\n"
            f"Directory: {skill.entry.directory}\n\n"
            f"{skill.body}"
        )

    def _review_changes(self, base: str | None = None, staged: bool = False) -> str:
        if self.reviewer is None:
            raise RuntimeError("reviewer is disabled by ENABLE_REVIEWER=0")
        return self.reviewer.review(base=base, staged=staged).text

    def _ensure_mcp_tools(self) -> None:
        if self._mcp_loaded:
            return
        self._mcp_loaded = True
        for definition in self.mcp.load_tools():
            if definition.openai_name in self.tool_functions:
                continue
            self.tools.append(definition.openai_tool())

            def call_mcp(
                *,
                tool_name: str = definition.openai_name,
                **arguments: Any,
            ) -> Any:
                return self.mcp.call_tool(tool_name, arguments)

            self.tool_functions[definition.openai_name] = call_mcp

    def run(
        self,
        user_input: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Run one user request through the ReAct loop.

        Args:
            user_input: The user's new message.
            on_event: Optional callback receiving high-level event dictionaries.
            cancel_event: Optional threading.Event checked at model/tool
                boundaries. Setting it stops the run with AgentInterrupted.
        """

        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be a non-empty string")

        self._ensure_mcp_tools()
        self.messages.append({"role": "user", "content": user_input})
        _emit_event(on_event, "thinking", "正在分析请求并规划下一步。", step=0)

        for step in range(1, self.max_steps + 1):
            _check_cancelled(cancel_event)
            _emit_event(
                on_event,
                "thinking",
                f"第 {step}/{self.max_steps} 轮：正在等待模型决定下一步。",
                step=step,
            )

            # On the final turn, omit tools. This keeps the hard turn limit while
            # giving the model a chance to summarize the evidence it already has.
            request: dict[str, Any] = {
                "model": self.model,
                "messages": self.messages,
            }
            if step < self.max_steps:
                request["tools"] = self.tools
                request["tool_choice"] = "auto"

            response = self.client.chat.completions.create(**request)
            _check_cancelled(cancel_event)
            message = response.choices[0].message
            content = _message_text(getattr(message, "content", None))
            raw_tool_calls = getattr(message, "tool_calls", None) or []
            tool_calls = [_tool_call_dict(call) for call in raw_tool_calls]

            # Preserve the assistant's tool-call request in the conversation.
            # The following tool messages reference these call IDs.
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            self.messages.append(assistant_message)

            if not tool_calls:
                answer = content or "The model returned an empty response."
                _emit_event(
                    on_event,
                    "final",
                    "Agent 已生成最终回答。",
                    content=answer,
                    step=step,
                )
                return answer

            # A provider should not emit tool calls when tools were omitted on
            # the final turn. Fail clearly rather than making an extra request.
            if step == self.max_steps:
                answer = content or (
                    f"Stopped after reaching max_steps={self.max_steps}. "
                    "The model did not produce a final answer."
                )
                _emit_event(
                    on_event,
                    "final",
                    "Agent 已达到最大轮次并返回当前结果。",
                    content=answer,
                    step=step,
                )
                return answer

            pending_image_messages: list[dict[str, Any]] = []
            pending_hook_messages: list[dict[str, Any]] = []
            for call in tool_calls:
                _check_cancelled(cancel_event)
                call_id = str(call.get("id", ""))
                function = call.get("function", {}) or {}
                name = str(function.get("name", ""))
                raw_arguments = function.get("arguments", "{}")

                if self.verbose:
                    print(
                        f"[step {step}/{self.max_steps}] tool={name} args={raw_arguments}",
                        file=sys.stderr,
                    )

                arguments: dict[str, Any] = {}
                snapshot = None
                diff_text = None
                post_tool = None
                try:
                    parsed_arguments = json.loads(raw_arguments or "{}")
                    if not isinstance(parsed_arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    arguments = parsed_arguments
                except (json.JSONDecodeError, ValueError) as exc:
                    tool_result: Any = {
                        "error": f"Invalid tool arguments: {exc}",
                    }
                else:
                    _emit_event(
                        on_event,
                        "tool_call",
                        _describe_tool_call(name, arguments),
                        step=step,
                        call_id=call_id,
                        tool=name,
                        arguments=arguments,
                    )
                    function_impl = self.tool_functions.get(name)
                    if function_impl is None:
                        tool_result = {"error": f"Unknown tool: {name}"}
                    else:
                        hook_decision = self.hooks.before_tool(name, arguments)
                        if not hook_decision.allowed:
                            tool_result = {
                                "error": f"Blocked by PreToolUse hook: {hook_decision.reason}",
                                "hook": "PreToolUse",
                                "reason": hook_decision.reason,
                                "alternative": hook_decision.alternative,
                            }
                        else:
                            snapshot = _snapshot_file_for_diff(name, arguments)
                            try:
                                tool_result = function_impl(**arguments)
                            except Exception as exc:  # Tool errors are model-visible.
                                tool_result = {
                                    "error": f"{type(exc).__name__}: {exc}",
                                }
                            else:
                                post_tool = self.hooks.after_tool(
                                    name,
                                    arguments,
                                    tool_result,
                                )
                                if post_tool and post_tool.get("status") == "red":
                                    pending_hook_messages.append(
                                        {
                                            "role": "user",
                                            "content": (
                                                "[PostToolUse]\n"
                                                + str(post_tool.get("summary", ""))
                                            ),
                                        }
                                    )

                tool_result_summary = _summarize_tool_result(tool_result)
                image_message = _image_user_message(tool_result)
                if image_message is not None:
                    pending_image_messages.append(image_message)

                diff_text = _build_file_diff(snapshot)
                self.trace.append(
                    {
                        "step": step,
                        "call_id": call_id,
                        "tool": name,
                        "arguments": arguments,
                        "result": tool_result_summary,
                        "diff": diff_text,
                        "post_tool": post_tool,
                    }
                )

                is_error = (
                    isinstance(tool_result_summary, dict)
                    and "error" in tool_result_summary
                )
                _emit_event(
                    on_event,
                    "tool_result",
                    f"工具 {name} 执行完成。",
                    step=step,
                    call_id=call_id,
                    tool=name,
                    arguments=arguments,
                    result=tool_result_summary,
                    is_error=is_error,
                    diff=diff_text,
                    post_tool=post_tool,
                )
                _check_cancelled(cancel_event)

                tool_content = _clip_tool_message(
                    result_to_text(tool_result_summary)
                )
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": tool_content,
                    }
                )

                if self.verbose:
                    preview = tool_content.replace("\n", " ")[:240]
                    print(f"[tool result] {preview}", file=sys.stderr)

            # Tool messages must immediately follow the assistant tool-call
            # message. Attach image content only after every tool result for the
            # current model turn has been appended.
            self.messages.extend(pending_image_messages)
            self.messages.extend(pending_hook_messages)

        # Defensive fallback. The final iteration normally returns above because
        # tools are omitted on the last turn.
        answer = (
            f"Stopped after reaching max_steps={self.max_steps}. "
            "No final answer was produced."
        )
        _emit_event(
            on_event,
            "final",
            "Agent 已停止并返回当前状态。",
            content=answer,
            step=self.max_steps,
        )
        return answer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive ReAct agent")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Maximum model turns per user request (default: 6)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name; defaults to OPENAI_MODEL or gpt-4.1-mini",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Working directory for tools (default: current directory)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print individual tool actions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Terminal chat entry point.

    Run from the project root:
        python -m agent.core

    Type a request, press Enter, and the agent will use tools as needed. Type
    /exit or /quit to leave. Context is retained across inputs in the same
    process.
    """

    _load_dotenv_if_available()
    args = build_arg_parser().parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        print(f"Workspace directory does not exist: {workspace}", file=sys.stderr)
        return 2
    os.chdir(workspace)

    try:
        agent = ReActAgent(
            max_steps=args.max_steps,
            model=args.model,
            verbose=not args.quiet,
        )
    except Exception as exc:
        print(f"Failed to start agent: {exc}", file=sys.stderr)
        return 1

    print(f"ReAct agent ready. model={agent.model} workspace={workspace}")
    print("Type /exit or /quit to stop.")

    while True:
        try:
            user_input = input("\nYou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"/exit", "/quit"}:
            break

        try:
            answer = agent.run(user_input)
        except Exception as exc:
            print(f"Agent error: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(f"\nAgent > {answer}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
