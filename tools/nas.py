#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
飞牛 NAS 远程操作小工具（paramiko）：在真机上跑命令、传文件。

用途：本项目的很多结论只能在真机上得到（加载器路径、glibc 版本、目录权限），
所以构建之外还需要一个能连机器排查/部署的工具。凭据一律从环境变量读，
不写死在代码里。

环境变量::

    NAS_HOST      默认 10.0.0.121
    NAS_USER      默认 zhao
    NAS_PASS      必填（或用 NAS_KEY 指定私钥）
    NAS_PORT      默认 22

用法::

    # 在 NAS 上跑一段脚本（推荐：脚本写文件再传，避免引号地狱）
    python tools/nas.py run --script build/_diag.sh
    python tools/nas.py run --cmd "uname -a"

    # 传文件
    python tools/nas.py put dist/emby_4.10.1.0_x86_native.fpk /vol1/emby.fpk

    # 一键部署并安装：上传 + 调应用中心安装接口
    python tools/nas.py deploy dist/emby_4.10.1.0_x86_native.fpk
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import paramiko
except ImportError:
    print("需要 paramiko：pip install paramiko")
    sys.exit(2)

HOST = os.environ.get("NAS_HOST", "10.0.0.121")
USER = os.environ.get("NAS_USER", "zhao")
PASSWORD = os.environ.get("NAS_PASS", "")
KEYFILE = os.environ.get("NAS_KEY", "")
PORT = int(os.environ.get("NAS_PORT", "22"))


def connect() -> paramiko.SSHClient:
    if not PASSWORD and not KEYFILE:
        raise SystemExit("请设置 NAS_PASS 或 NAS_KEY 环境变量")
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, port=PORT, username=USER,
                password=PASSWORD or None,
                key_filename=KEYFILE or None,
                timeout=20, banner_timeout=30, auth_timeout=30,
                look_for_keys=False, allow_agent=False)
    return cli


def run(cli: paramiko.SSHClient, cmd: str, timeout: int = 300) -> int:
    _in, out, err = cli.exec_command(cmd, timeout=timeout)
    text = out.read().decode("utf-8", "replace")
    errt = err.read().decode("utf-8", "replace")
    if text:
        print(text)
    # NAS 上 zhao 没有 home 目录，ssh 每次都会提示一句，属噪音，过滤掉
    errt = "\n".join(l for l in errt.splitlines()
                     if "Could not chdir to home directory" not in l)
    if errt.strip():
        print("--- stderr ---")
        print(errt)
    return 0


def put(cli: paramiko.SSHClient, local: str, remote: str) -> int:
    size = os.path.getsize(local)
    print(f"上传 {local} ({size / 1048576:.1f} MB) -> {remote}")
    sftp = cli.open_sftp()
    t0 = time.time()
    last = [t0]

    def progress(done: int, total: int) -> None:
        now = time.time()
        if now - last[0] >= 2 or done == total:
            print(f"  {done * 100.0 / total if total else 0:5.1f}%  "
                  f"{done / 1048576:7.1f} MB")
            last[0] = now

    sftp.put(local, remote, callback=progress)
    sftp.close()
    print(f"完成，用时 {time.time() - t0:.0f}s")
    return 0


# 应用中心安装接口：飞牛把「手动安装」做成后端任务，这里直接投递 fpk 路径。
# 上传到 /vol1 后调用它，等价于在 UI 里点「手动安装」。装完会有进度条。
INSTALL_HINT = """
请在应用中心手动安装（当前 SSH 用户不是 root，无法直接调用安装接口）：
   应用中心 -> 手动安装 -> 选择 {remote}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="在 NAS 上执行命令/脚本")
    p_run.add_argument("--cmd", default="")
    p_run.add_argument("--script", default="")
    p_run.add_argument("--timeout", type=int, default=300)

    p_put = sub.add_parser("put", help="上传文件")
    p_put.add_argument("local")
    p_put.add_argument("remote")

    p_dep = sub.add_parser("deploy", help="上传 fpk 并给出安装指引")
    p_dep.add_argument("fpk")
    p_dep.add_argument("--remote", default="/vol1/emby.fpk")

    args = ap.parse_args()
    cli = connect()
    try:
        print(f"已连接 {USER}@{HOST}")
        if args.cmd == "run":
            if args.script:
                with open(args.script, encoding="utf-8") as f:
                    code = f.read()
            else:
                code = args.cmd
            return run(cli, code, args.timeout)
        if args.cmd == "put":
            return put(cli, args.local, args.remote)
        if args.cmd == "deploy":
            put(cli, args.fpk, args.remote)
            print(INSTALL_HINT.format(remote=args.remote))
            return 0
    finally:
        cli.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
