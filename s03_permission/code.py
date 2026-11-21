#!/usr/bin/env python3
"""
s03_permission.py - Permission System

Three gates inserted before tool execution:

    Gate 1: Hard deny list (rm -rf /, sudo, ...)
    Gate 2: Rule matching (write outside workspace? destructive cmd?)
    Gate 3: User approval (pause and wait for confirmation)

    +----------+      +-------+      +--------------+      +---------------+
    |   User   | ---> |  LLM  | ---> | Permission   | ---> | Tool Dispatch |
    |  prompt  |      |       |      | 1. deny list |      | execute       |
    +----------+      +---+---+      | 2. rules     |      +-------+-------+
                          ^          | 3. approval  |              |
                          |          +------+-------+              |
                          |                 | deny                 |
                          |                 v                      v
                          |          +-------------------------------+
                          +----------+ tool_result: denied or output |
                                     +-------------------------------+

Only one line added to the agent loop:

    if not check_permission(block):
        continue

Builds on s02 (multi-tool). Usage:

    python s03_permission/code.py
    Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
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
    "Use tools to solve tasks. Do not ask for approval yourself. "
    "For destructive operations, still request the tool call; "
    "the harness will approve or deny before execution."
)


# -- From s02: tool implementations --

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
        lines = (WORKDIR / path).resolve().read_text(encoding="utf-8").splitlines()
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


# -- From s02 (unchanged): tool definitions and dispatch --

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


# -- New in s03: three-gate permission pipeline --

# Gate 1: Hard deny list - always forbidden
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if=", "> /dev/sda"]

def check_deny_list(command: str) -> str | None:
    # command 是模型交给 bash 的整条命令字符串。
    # DENY_LIST 是预先定义好的列表，里面有 "sudo" 等禁止项。
    for pattern in DENY_LIST:
        # in 做的是区分大小写的子串匹配。
        # 比如 "sudo" in "sudo whoami" 的结果是 True。
        if pattern in command:
            # 找到第一项就返回原因，函数在这里结束。
            return f"命中硬拒绝列表：{pattern}"

    # 循环走完还没有命中，才走到这里。
    # None 表示这一关没拦住，外层还会继续检查审批规则。
    return None


# Gate 2: Rule matching - context-dependent checks
# 相邻的字符串会拼成一条正则。这里拆成几段，方便读每一部分。
DESTRUCTIVE_COMMAND_WORD = re.compile(
    r"(?i)"                    # 忽略大小写，DEL 和 del 都能匹配。
    r"(?:^|[;&|()\n])"         # 从字符串开头，或这些分隔符之后开始找。
    r"\s*"                     # 命令前允许有零个或多个空白字符。
    r"(?:rm|del)"              # 匹配 rm 或 del。
    r"(?=\s|$|[;&|()])"        # 后面必须是空白、字符串结尾或指定分隔符。
                              # (?=...) 只检查后面是什么，不把它算进匹配结果。
)

# 匹配 rm 和 del
def contains_destructive_command(command: str) -> bool:
    # search() 找到匹配时返回 Match 对象，找不到时返回 None。
    match = DESTRUCTIVE_COMMAND_WORD.search(command)

    # 转成布尔值，让规则表可以直接用它做判断。
    return match is not None


PERMISSION_RULES = [
    {"tools": ["read_file", "write_file", "edit_file"],
     
     # lambda args: 表达式 定义了一个接收 args 的函数，并把表达式的结果作为返回值。
     # 这里 args 是工具调用的参数字典，例如 {"path": "../notes.txt"}。
     # 1. 取出工具传来的路径。字典里没有 "path" 时，默认用 ""。
     # 2. WORKDIR 是一个 Path 对象。这里的 / 用来拼接路径。
     #    相对路径会接到 WORKDIR 后面，绝对路径则直接使用。
     # 3. .resolve() 得到解析后的绝对路径，处理 ..、. 和符号链接。
     # 4. .is_relative_to(WORKDIR) 判断目标是否等于工作区目录，或位于工作区目录下面。
     "check": lambda args: not (WORKDIR / args.get("path", "")).resolve().is_relative_to(WORKDIR),
     "message": "Writing outside workspace"},
    {"tools": ["bash"],
     "check": lambda args: contains_destructive_command(args.get("command", "")) or
     any(kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]),
     "message": "Potentially destructive command"},
]

def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        # 工具名不在这一条规则的适用范围内，就去看下一条。
        # 例如 read_file 不会执行 bash 那条规则的 lambda。
        # 取出规则里的函数，传入本次工具调用的参数字典
        # rule 是 PERMISSION_RULES 列表里的一条规则，也就是一个 Python 字典。
        # `rule["tools"]`、`rule["check"]`、`rule["message"]` 分别取出字典里的三个值。
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# Gate 3: User approval - wait for confirmation after rule match
def ask_user(tool_name: str, args: dict, reason: str) -> str:

    # 先显示为什么需要审批，再显示实际要执行的工具和参数。
    print(f"\n\033[33m[permission] {reason}\033[0m")
    print(f"   Tool: {tool_name}({args})")

    # input() 会停在这里，等用户按回车提交输入。
    # strip() 去掉首尾空白，lower() 转成小写。
    choice = input("   Allow? [y/N] ").strip().lower()

    # 例如 " YES " 处理后变成 "yes"，会被批准。 
    # 直接回车得到空字符串。空字符串和其他输入都默认拒绝。
    return "allow" if choice in ("y", "yes") else "deny"


# Pipeline: all three gates chained
# 串起前面的检查
# block 是模型提出的一次工具调用。
# block.name 是工具名，block.input 是参数字典。
def check_permission(block) -> bool:

    # 第一关：只有 bash 查硬拒绝列表。
    if block.name == "bash":
        reason = check_deny_list(block.input.get("command", ""))
        if reason:
            print(f"\n\033[31m[blocked] {reason}\033[0m")
            return False

    # 第二关：判断这次调用是否触发审批规则。
    reason = check_rules(block.name, block.input)
    if reason:

        # 第三关：把命中原因传给终端，由用户决定。
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return False
    return True


# -- Agent loop: same as s02, with check_permission() inserted --

def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        results = []
        for block in tool_calls:
            print(f"\033[36m> {block.name}\033[0m")

            # s03 change: run through permission pipeline before executing
            if not check_permission(block):
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": "Permission denied."})
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s03: Permission")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    while True:
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            query = input("\001\033[36m\002s03 >> \001\033[0m\002")
        except (EOFError, KeyboardInterrupt):
            break
        if not query.strip():
            continue
        if query.strip().lower() in ("q", "exit"):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
