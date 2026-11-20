#!/usr/bin/env python3
"""
s02_tool_use.py - Tools

The agent loop from s01 does not change. This lesson adds four tools
and a dispatch map:

    +----------+      +-------+      +--------------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch            |
    |  prompt  |      |       |      | bash       -> run_bash   |
    +----------+      +---+---+      | read_file  -> run_read   |
                          ^          | write_file -> run_write  |
                          |          | edit_file  -> run_edit   |
                          +----------+ glob       -> run_glob   |
                          tool_result+--------------------------+

  + run_read / run_write / run_edit / run_glob
  + TOOL_HANDLERS instead of a hard-coded run_bash call
  + safe_path to keep file tools inside the workspace

Key insight: the loop stays the same; only tool registration and dispatch grow.
"""

import os
import subprocess
from pathlib import Path

# 配置终端输入，使 Readline 能正确处理多字节字符（如中文）。
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

# 从 .env 加载模型配置。使用自定义 API 地址时，移除环境中的认证令牌。
load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# 把当前平台的命令语法和工作目录写入系统提示，供模型选择工具和构造参数。
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


# Shell 工具：沿用 s01 的实现，先检查命令，再执行并合并标准输出和错误输出。

def run_bash(command: str) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    """
    for d in dangerous:
        if d in command:
            return "Error: Dangerous command blocked"
    下面代码展开
    """
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace",
                           timeout=120)
        out = (r.stdout + r.stderr).strip() #strip()作用是去除前后的空字符
        return out[:50000] if out else "(no output)" #out不为NULL，返回前50000个字符

    #except的作用：捕获并处理 try 中发生的异常。
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# 文件工具共用的路径检查。解析相对路径和符号链接后，确认目标仍在工作目录内。

def safe_path(p: str) -> Path:

    # WORKDIR / p：拼接工作目录和输入路径。这里的 / 是 Path 的路径拼接运算符。
    # .resolve()：转换为绝对路径，解析 .. 等路径片段以及符号链接。
    path = (WORKDIR / p).resolve()

    # 判断解析后的路径是否位于工作目录中，或就是工作目录本身。
    if not path.is_relative_to(WORKDIR):

        # raise：抛出异常
        raise ValueError(f"Path escapes workspace: {p}")
    return path


# 文件读写统一使用 UTF-8。读取可限制返回行数，写入会自动创建缺失的父目录。

""" -- path: str 是要读取的文件路径，例如 "notes.txt"。
    -- limit: int | None = None 表示可以传入整数，也可以传 None。省略时默认是 None，会返回全部行。
    -- -> str 表示返回值是字符串。"""
def run_read(path: str, limit: int | None = None) -> str:
    try:
        """ --- safe_path()，解析路径并确认它位于工作目录内。
            --- .read_text(encoding="utf-8") 把整个文件读成一个字符串。
            --- .splitlines() 按换行拆成字符串列表，并去掉每行的换行符。"""
        lines = safe_path(path).read_text(encoding="utf-8").splitlines()

        # 只用当limit不为NULL 同时 limit < 所读到的行数，才能截取
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"

# 这个函数把 content 写入工作目录中的指定文件。父目录不存在就创建，文件已存在就覆盖。

def run_write(path: str, content: str) -> str:
    try:
        file_path = safe_path(path) # 检查文件路径。

        # 创建父目录。exist_ok=True 允许目录已经存在。
        file_path.parent.mkdir(parents=True, exist_ok=True) 
        file_path.write_text(content, encoding="utf-8") # 写入文件。
        return f"Wrote {len(content)} bytes to {path}" #写入成功就返回 
    except Exception as e: # 捕获异常
        return f"Error: {e}"


# 局部编辑按原文精确匹配，只替换第一次出现的位置。找不到原文时直接返回错误。
def run_edit(path: str, old_text: str, new_text: str) -> str:
    try:
        file_path = safe_path(path) # 检验路径
        text = file_path.read_text(encoding="utf-8") # 读取原文件
        if old_text not in text: # 如果原文找不到old_text，就会返回错误信息
            return f"Error: text not found in {path}"

        # 若存在，就会进行文本替换：new_text 替换 old_text
        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# 按模式递归搜索文件，过滤工作目录外的匹配项，并限制返回数量。
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


# 工具声明会随请求发送给模型，说明有哪些工具、参数类型以及哪些参数必填。

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

# 分发表把模型返回的工具名映射到本地函数，新增工具时可直接注册对应的处理函数。

TOOL_HANDLERS = {
    "bash": run_bash, "read_file": run_read, "write_file": run_write,
    "edit_file": run_edit, "glob": run_glob,
}


# Agent 循环负责在模型和本地工具之间传递消息，直到模型不再请求调用工具。
def agent_loop(messages: list):
    while True:
        # 将完整对话和工具声明发给模型，并把本轮回复加入历史。
        response = client.messages.create(
            model=MODEL, system=SYSTEM, messages=messages,
            tools=TOOLS, max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        """ --- for block in response.content：逐个遍历回复中的内容块。
            --- if block.type == "tool_use"：只保留“工具调用”类型的块。
            --- 开头的 block：将符合条件的块本身放进新列表。"""
        tool_calls = [
            block for block in response.content if block.type == "tool_use"
        ]
        if not tool_calls:
            return

        # 执行本轮的工具调用，用调用 ID 将每条结果与原请求对应，再一起交回模型。
        results = []
        for block in tool_calls:
            print(f"\033[33m> {block.name}\033[0m")
            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"
            print(str(output)[:200])
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})

        messages.append({"role": "user", "content": results})


# 命令行入口维护多轮对话历史，并在每轮工具调用结束后打印模型的文字回复。
if __name__ == "__main__":
    print("s02: Tool Use - four tools added to s01")
    print("Enter a question, press Enter to send. Type q to quit.\n")

    history = []
    while True:
        try:
            # \001/\002 tell Readline the ANSI escapes have zero display width.
            query = input("\001\033[36m\002s02 >> \001\033[0m\002")
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
