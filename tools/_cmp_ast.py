"""对比「备份目录」与当前工作区版本：剥离 docstring 后 AST 是否完全一致。

用法：
    python tools/_cmp_ast.py <备份目录> <文件...>       # 文件为相对仓库根的路径
    python tools/_cmp_ast.py                           # 默认对 git HEAD 比（见下方 FILES）

只用来验证"本次改动仅涉及注释/文档字符串"。若文件在 git 中不存在（未跟踪），
就在改动前先 `cp` 一份到临时目录，再用第一个用法比对。
"""

import ast
import os
import subprocess
import sys

DOC_OWNERS = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

DEFAULT_FILES = [
    "custom_components/ir_hub/const.py",
    "custom_components/ir_hub/ir_command.py",
    "custom_components/ir_hub/library.py",
    "custom_components/ir_hub/__init__.py",
]


def strip_docstrings(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, DOC_OWNERS):
            continue
        body = list(node.body)
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            rest = body[1:]
            node.body = rest if rest else [ast.Pass()]
    return tree


def from_git(ref: str, path: str) -> str:
    return subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


args = sys.argv[1:]
baseline = None
if args and os.path.isdir(args[0]):
    baseline, args = args[0], args[1:]
files = args or DEFAULT_FILES

bad = 0
for path in files:
    try:
        old_src = (
            from_git("HEAD", path) if baseline is None
            else read(os.path.join(baseline, os.path.basename(path)))
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as err:
        print(f"SKIP  {path}（取不到基线：{err}）")
        bad += 1
        continue
    same = ast.dump(strip_docstrings(old_src), include_attributes=False) == ast.dump(
        strip_docstrings(read(path)), include_attributes=False
    )
    if not same:
        bad += 1
    print(f"{'SAME ' if same else 'DIFF '} {path}")

print(f"\nsummary: {len(files) - bad}/{len(files)} 代码结构一致（仅注释/docstring 变化）")
sys.exit(1 if bad else 0)
