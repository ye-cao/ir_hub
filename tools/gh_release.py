#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发版助手：推 tag 之后创建 GitHub Release（HACS 只认 Release，光有 tag 看不到更新）。

    PY="C:/Users/ye_ca/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

    $PY tools/gh_release.py inspect 0.3.6                       # 看已有 release 的写法
    $PY tools/gh_release.py create 0.3.7 <body.md> "0.3.7 标题"   # 建（tag 可已存在）

凭据从**本机 git 凭据管理器**取（`git credential fill`），不读环境变量、不落盘、
不打印 —— 所以脚本本身可以进仓库。⚠️ 不要改成"把 token 写进常量"。

正常发版顺序：
  1. 改 `manifest.json` 的 version
  2. 跑 DELIVERY.md「发版前必跑」的四个校验
  3. `git commit` → `git push origin main`
  4. `git tag <version> && git push origin <version>`
  5. **本脚本**建同名 Release（漏了这步，HACS 不会提示更新）
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "ye-cao/ir_hub"
API = f"https://api.github.com/repos/{REPO}"


def get_token() -> str:
    """从本机 git 凭据管理器取 github.com 的 token（只在内存里流转）。"""
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):]
    raise SystemExit("凭据管理器里没找到 github.com 的凭据 —— 先手动 git push 一次把它存进去")


def call(method: str, path: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ir-hub-release-helper",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise SystemExit(f"HTTP {exc.code} {exc.reason}\n{body[:800]}") from exc


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    mode, tag = sys.argv[1], sys.argv[2]
    token = get_token()
    print(f"[凭据已取到（长度 {len(token)}，不回显）]")

    if mode == "inspect":
        rel = call("GET", f"/releases/tags/{tag}", token)
        print("name      :", rel.get("name"))
        print("tag_name  :", rel.get("tag_name"))
        print("draft     :", rel.get("draft"), "| prerelease:", rel.get("prerelease"))
        print("target    :", rel.get("target_commitish"))
        print("published :", rel.get("published_at"))
        print("--- body ---")
        print(rel.get("body"))
        return 0

    if mode == "create":
        if len(sys.argv) < 4:
            raise SystemExit("用法：create <tag> <body.md> [release 标题]")
        with open(sys.argv[3], encoding="utf-8") as handle:
            body = handle.read()
        rel = call(
            "POST",
            "/releases",
            token,
            {
                "tag_name": tag,
                # 命名惯例照旧版：`<版本> <短标题>`（如 "0.3.6 按键码库官网 1:1"）
                "name": sys.argv[4] if len(sys.argv) > 4 else tag,
                "body": body,
                "draft": False,
                "prerelease": False,
            },
        )
        print("已创建:", rel["html_url"])
        print("name:", rel["name"], "| tag:", rel["tag_name"], "| id:", rel["id"])
        return 0

    raise SystemExit(f"未知模式 {mode!r}（只支持 inspect / create）")


if __name__ == "__main__":
    sys.exit(main())
