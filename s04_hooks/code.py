#!/usr/bin/env python3
"""
s04_hooks.py - Hooks

Hooks run callbacks at fixed points in the agent loop:

    User prompt
         |
         v
    UserPromptSubmit
         |
         v
    +----------+      +-------+      +------------+      +-------+
    | messages | ---> |  LLM  | ---> | PreToolUse | ---> | Tool  |
    +----------+      +---+---+      | permission |      +---+---+
         ^                | stop     | log        |          |
         |                v          +------------+          v
         |            Stop hook                         PostToolUse
         |                                               |
         +---------------- tool_result ------------------+
"""

import os
import re
import subprocess
from pathlib import Path

try:
    import readline
    readline.parse_and_bind('set bind-tty-special-chars off')
    readline.parse_and_bind('set input-meta on')
    readline.parse_and_bind('set output-meta on')
    readline.parse_and_bind('set convert-meta off')
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

ENVIRONMENT_PROMPT = (
    "Windows: the bash tool runs through cmd.exe; use cmd.exe syntax, not Unix "
    "Bash or PowerShell syntax, and prefer dedicated file tools for file operations"
    if os.name == "nt"
    else "Unix-like: the bash tool runs the system shell"
)
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Environment: {ENVIRONMENT_PROMPT}. "
    "Use tools to solve tasks. Act, don't explain."
)


# -- From s02-s03: tool implementations --

def run_bash(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace", timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"

def run_read(path: str, limit: int | None = None) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        lines = file_path.read_text(encoding="utf-8").splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

def run_write(path: str, content: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"

def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = (WORKDIR / path).resolve()
        text = file_path.read_text(encoding="utf-8")
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"

def run_glob(pattern: str) -> str:
    import glob as g
    try:
        matches = sorted({
            match for match in g.glob(
                pattern, root_dir=WORKDIR, recursive=True)
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR)
        })
        shown = matches[:200]
        if len(matches) > 200:
            shown.append("... (more matches omitted; narrow the pattern)")
        return "\n".join(shown) if shown else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

# properties 预先定义工具的参数名和类型，required 指定必填参数。
# 模型发起工具调用时提供具体参数值，代码通过 block.input 读取。
TOOLS = [
    {"name": "bash", "description": "Run a shell command.",
     "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}},
    {"name": "read_file", "description": "Read file contents.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write content to a file.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit_file", "description": "Replace exact text in a file once.",
     "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}},
    {"name": "glob", "description": "Find files matching a glob pattern; ** matches recursively.",
     "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}},
]

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# -- New in s04: hook system (s03 permission logic now uses hooks) --

HOOKS = {
    "UserPromptSubmit": [],  # 用户提交输入后、发给模型前触发。
    "PreToolUse": [],        # 每次工具执行前触发，可拦截本次调用。
    "PostToolUse": [],       # 工具返回结果后、结果发给模型前触发。
    "Stop": [],              # 模型未请求工具、本轮循环即将退出时触发，可让循环继续。
}


def register_hook(event: str, callback):
    """将回调函数注册到指定的钩子事件。

    event 是 HOOKS 中的事件名，例如 "PreToolUse"。
    callback 是事件触发时调用的函数，其参数须与该事件传入的参数匹配。
    """
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """按注册顺序执行指定事件的回调。触发指定事件，按注册顺序执行它的回调函数。

    event 用于查找回调列表，*args 收集事件附带的位置参数并传给每个回调。
    遇到第一个非 None 的结果时，立即返回该结果，跳过剩余回调。
    列表为空或所有回调都返回 None 时，返回 None。

    例如，PreToolUse 的权限检查返回拒绝消息时，后续日志回调会被跳过，
    Agent 循环收到消息后会跳过本次工具调用。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


# s03 permission check logic, now wrapped as a hook
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)(?:^|[;&|()\n])\s*(?:rm|del)(?=\s|$|[;&|()])"
)
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]


def contains_destructive_command(command: str) -> bool:
    return bool(DESTRUCTIVE_COMMAND_WORD.search(command))

# 在工具执行前检查权限。命中拒绝列表的 Bash 命令会直接被拦截，
# 其他潜在破坏性命令和工作目录外的文件访问需要用户确认。
# 返回拒绝原因会阻止本次调用，返回 None 会继续执行后续钩子。
def permission_hook(block):
    """PreToolUse: s03 check_permission() logic moved here."""
    # block.name 是工具名，block.input 是参数字典，键由各工具的 input_schema 定义。
    if block.name == "bash":
        # bash 用 command 传入要执行的命令。get 的第二个参数是缺少该键时的默认值。
        command = block.input.get("command", "")
        # 逐项检查拒绝列表。命中任一子串时，打印拦截提示并立即返回拒绝原因。
        for pattern in DENY_LIST:
            if pattern in command:
                print(f"\n\033[31m[blocked] '{pattern}'\033[0m")
                return "Permission denied by deny list"
        # 正则匹配到潜在破坏性命令，或命令包含任一风险关键词时，要求用户确认。
        if contains_destructive_command(command) or any(
            kw in command for kw in DESTRUCTIVE
        ):
            print(f"\n\033[33m[permission] Potentially destructive command\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            # 去掉首尾空白并转为小写。只有 y 或 yes 表示同意，直接回车表示拒绝。
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    if block.name in ("read_file", "write_file", "edit_file"):
        # 文件工具用 path 传入目标文件的路径。缺少该键时，get 返回空字符串。
        path = block.input.get("path", "")
        # 先解析 .. 和符号链接，再判断目标是否位于工作目录内。
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            # 访问工作目录外的路径时，显示工具及参数，并使用相同的确认规则。
            print(f"\n\033[33m[permission] Access outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None  # 当前钩子放行，继续执行同一事件的后续回调。

# 工具执行前，打印工具名和参数预览，方便查看模型发起的调用。
def log_hook(block):
    """PreToolUse: log every tool call."""
    # 取参数字典的前两个值，转成列表的字符串表示，再截取前 60 个字符。
    args_preview = str(list(block.input.values())[:2])[:60]
    # \033[90m 将终端文字设为灰色，\033[0m 在打印后恢复默认样式。
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None  # 钩子分发器会继续执行后续回调。

# 工具执行后，输出超过 10 万字符时打印提醒。
def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    # str(output) 将工具输出转成字符串，len() 统计字符数。
    if len(str(output)) > 100000:
        # 用黄色显示工具名和输出长度。末尾的 \033[0m 重置终端样式。
        print(f"\033[33m[HOOK] Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None  # 提醒打印在终端，工具结果仍由 Agent 循环传给模型。

# 用户输入发给模型前，在终端打印当前工作目录。
def context_inject_hook(query: str):
    # query 是用户提交的文本。这个回调只用 WORKDIR 拼接日志内容。
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None  # 回调执行结束，调用方会继续发送用户输入。

# 本轮 Agent 循环即将结束时，打印对话历史中的工具结果总数。
def summary_hook(messages: list):
    # 外层遍历历史消息，内层只遍历 content 为列表的消息，否则用空列表跳过。
    # 每遇到一个类型为 tool_result 的字典就计 1，sum() 将这些 1 加成总数。
    tool_count = sum(1 for m in messages
                     for b in (m.get("content") if isinstance(m.get("content"), list) else [])
                     if isinstance(b, dict) and b.get("type") == "tool_result")
    # 被权限拦截的调用也会产生 tool_result，因此同样计入这个数字。
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None  # 这个回调不要求 Agent 再跑一轮。

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# -- Agent loop: same structure as s03, but no hard-coded check --
# s03: if not check_permission(block): ...
# s04: if trigger_hooks("PreToolUse", block): ...

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [
            # 前面的 block 决定往列表里放什么，后面的 if 决定哪些对象能放进去。
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        for block in tool_calls:
            # s04 change: hook replaces hard-coded check_permission()
            """这会触发工具执行前的钩子。
            比如权限钩子返回了 "Permission denied by user"，
            `if blocked:` 就会成立。程序把拒绝原因用 str(blocked) 转成文本，
            连同 block.id 一起存入 results，然后执行 continue。
            这样本轮循环就不会执行该工具，直接跳到下一个 block。"""
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(blocked)})
                # continue 跳到 for 循环的下一个工具调用。 当前工具不会执行，也不会触发 PostToolUse
                continue

            """
            假设 block.name 是 "read_file"，`block.input` 是 {"path": "README.md"}。
            第一行从字典里找到 run_read，第二行用 ** 把参数字典展开，相当于执行 run_read(path="README.md")。
            找不到对应函数时，`output` 就是一条 Unknown 提示。
            """
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            """
            block、output是前面执行工具调用的结果。接下来触发 PostToolUse 钩子，让后置钩子看到结果。
            例如 large_output_hook 会在输出超过 10 万字符时打印提醒。
            执行到这里时，工具函数已经跑完了。`block` 是模型发出的那次工具调用，`output` 是工具函数刚返回的结果。
            接下来程序做两件事：让后置钩子看到结果，再把结果存起来，等这一批工具都处理完后发回模型。
            """
            trigger_hooks("PostToolUse", block, output)  # s04: post hook
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s04: Hooks - extension logic on hooks, loop stays clean")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    while True:
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            query = input("\001\033[36m\002s04 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            continue
        if query.strip().lower() in ("q", "exit"):
            break
        trigger_hooks("UserPromptSubmit", query)
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            # 如果 block 有 type 属性，就返回 block.type。如果没有，就返回 None。
            if getattr(block, "type", None) == "text": 
                print(block.text)
        print()
