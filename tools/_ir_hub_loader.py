"""把 `custom_components/ir_hub` 当包导入（供 tools/ 下的离线脚本复用）。

`ir_hub/__init__.py` 会 import homeassistant（开发机没有），所以**不能**走真包路径：
只挂一个空壳包并让它的 `__path__` 指向组件目录，再按需加载子模块 ——
子模块之间是相对 import（`from .library import ...`），借此自然成立。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COMPONENT = os.path.join(ROOT, "custom_components", "ir_hub")
PKG = "_ir_hub_shim"


def install() -> None:
    """挂包外壳（幂等）。"""
    if PKG in sys.modules:
        return
    shim = types.ModuleType(PKG)
    shim.__path__ = [COMPONENT]
    sys.modules[PKG] = shim


def load(name: str):
    """按模块名加载组件子模块，如 `library` / `ac_library` / `learn_match_ac`。"""
    install()
    full = f"{PKG}.{name}"
    cached = sys.modules.get(full)
    if cached is not None:
        return cached
    path = os.path.join(COMPONENT, name + ".py")
    spec = importlib.util.spec_from_file_location(full, path)
    assert spec and spec.loader, f"找不到模块 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module
