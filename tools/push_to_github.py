#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把本仓库推到 GitHub（创建仓库 + push），全程不落盘 token。

安全设计
--------
* token 从 Windows 凭据管理器或环境变量读进内存
* push 时通过临时目录里的 `GIT_ASKPASS` 脚本提供密码；该脚本只读环境变量
  `EMBY_GH_TOKEN`，本身不含 token；用完立即删除
* 不写入 `.git/config`（用 `-c credential.helper=` 关掉持久化）
* 不把 token 放进任何命令行参数（`ps` 可见）
* 任何输出都只打印长度/前缀
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

REPO = os.environ.get("EMBY_GH_REPO", "emby-fnos")
OWNER_FALLBACK = "MinimaxFlora"


def read_credential(target: str = "git:https://github.com") -> tuple[str, str]:
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
            ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    ptr = ctypes.c_void_p()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(ptr)):
        raise RuntimeError("凭据管理器里没有 GitHub 凭据")
    try:
        cred = ctypes.cast(ptr, ctypes.POINTER(CREDENTIAL)).contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize) \
            if cred.CredentialBlobSize else b""
        return cred.UserName, blob.decode("utf-16-le", "ignore")
    finally:
        advapi.CredFree(ptr)


def get_token() -> str:
    for n in ("GH_TOKEN", "GITHUB_TOKEN", "EMBY_GH_TOKEN"):
        v = os.environ.get(n, "").strip()
        if v:
            return v
    if os.name == "nt":
        return read_credential()[1].strip()
    raise SystemExit("请设置 GH_TOKEN 环境变量")


def api(path: str, token: str, method: str = "GET", body: dict | None = None):
    url = path if path.startswith("http") else f"https://api.github.com{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "emby-fnos-builder",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {"message": raw[:400]}


def git(*args: str, cwd: str, env: dict | None = None, check: bool = True):
    r = subprocess.run(["git", *args], cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：\n{r.stdout}\n{r.stderr}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--desc", default="Emby for 飞牛 fnOS 原生安装包（.fpk）构建工具链与 GitHub Actions")
    ap.add_argument("--no-create", action="store_true", help="仓库已存在，只推送")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    token = get_token()
    print(f"token: {len(token)} 字符，前缀 {token[:8]}...")

    st, me = api("/user", token)
    if st != 200:
        raise SystemExit(f"token 校验失败：{st} {me}")
    owner = me["login"]
    print(f"账号: {owner}")

    full = f"{owner}/{args.repo}"

    # 1. 建仓库（已存在则复用）
    if not args.no_create:
        st, res = api("/user/repos", token, "POST", {
            "name": args.repo,
            "description": args.desc,
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if st == 201:
            print(f"已创建仓库 {full}")
        elif st == 422:
            print(f"仓库已存在，复用 {full}")
        else:
            raise SystemExit(f"创建仓库失败：{st} {res}")

    # 2. 本地 git 初始化
    if not os.path.isdir(os.path.join(root, ".git")):
        git("init", "-b", "main", cwd=root)
        print("已初始化本地仓库（分支 main）")

    git("add", "-A", cwd=root)
    r = git("diff", "--cached", "--name-only", cwd=root)
    files = [f for f in r.stdout.splitlines() if f.strip()]
    print(f"待提交 {len(files)} 个文件")

    # 敏感文件体检
    banned = [f for f in files
              if f.startswith(("_work/", "_tools/", "_ref/", "dist/"))
              or f.endswith(".fpk")
              or "provision" in f.lower()
              or f.startswith("emby/app/")]
    if banned:
        raise SystemExit("以下不该提交，请检查 .gitignore：\n  " + "\n  ".join(banned[:10]))

    r = git("log", "--oneline", "-1", cwd=root, check=False)
    if r.returncode != 0:
        git("commit", "-m",
            "feat: Emby for 飞牛 fnOS 原生安装包构建工具链\n\n"
            "- 原生安装（非 Docker），Launcher 显式调用系统加载器\n"
            "- 按真机结论拆分库目录，剔除随包 libc/loader\n"
            "- 官方图标（透明背景，与官方包逐字节一致）\n"
            "- GitHub Actions 在 Linux runner 上构建两个架构",
            cwd=root)
        print("已提交")
    else:
        r2 = git("status", "--porcelain", cwd=root)
        if r2.stdout.strip():
            git("commit", "-m", "chore: 更新构建脚本与文档", cwd=root)
            print("已提交增量改动")
        else:
            print("没有需要提交的改动")

    # 3. push：用临时 askpass，token 不落盘
    tmp = tempfile.mkdtemp(prefix="emby-gh-")
    try:
        if os.name == "nt":
            askpass = os.path.join(tmp, "askpass.bat")
            with open(askpass, "w", encoding="ascii", newline="\r\n") as f:
                f.write("@echo off\r\necho %EMBY_GH_TOKEN%\r\n")
        else:
            askpass = os.path.join(tmp, "askpass.sh")
            with open(askpass, "w", encoding="ascii") as f:
                f.write('#!/bin/sh\nprintf %s "$EMBY_GH_TOKEN"\n')
            os.chmod(askpass, 0o700)

        env = dict(os.environ)
        env["EMBY_GH_TOKEN"] = token
        env["GIT_ASKPASS"] = askpass
        env["GIT_TERMINAL_PROMPT"] = "0"

        url = f"https://github.com/{full}.git"
        git("remote", "remove", "origin", cwd=root, check=False)
        git("remote", "add", "origin", url, cwd=root)
        r = git("push", "-u", "origin", "main",
                "-c", "credential.helper=", cwd=root, env=env, check=False)
        print(r.stdout)
        if r.returncode != 0:
            print(r.stderr)
            raise SystemExit("push 失败")
        print(f"\n完成：https://github.com/{full}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
