#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
管理 GitHub 仓库的 Actions secrets / variables（值不落盘、不回显）。

用法::

    python tools/gh_secrets.py list
    python tools/gh_secrets.py set-secret EMBY_LICENSE_JSON --value-file _local/x.json
    python tools/gh_secrets.py set-var  EMBY_LICENSE_HOSTS --value "1.2.3.4 example.com"
    python tools/gh_secrets.py delete-secret EMBY_LICENSE_JSON
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402

REPO = os.environ.get("EMBY_GH_REPO_FULL", "MinimaxFlora/emby-fnos")


def call(path: str, token: str, method: str = "GET", body=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"https://api.github.com{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "emby-fnos-builder",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            payload = r.read()
            if raw or not payload.strip():
                return r.status, payload
            return r.status, json.loads(payload.decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def libsodium_seal(public_key_b64: str, secret: str) -> str:
    """用仓库公钥对 secret 做 libsodium sealed box 加密（GitHub 要求）。"""
    try:
        from nacl import encoding, public  # type: ignore
    except ImportError:
        raise SystemExit("需要 PyNaCl：pip install pynacl")
    pk = public.PublicKey(public_key_b64.encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(secret.encode())
    return base64.b64encode(sealed).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)

    sub.add_parser("list")
    for kind in ("secret", "var"):
        for op in ("get", "set", "delete"):
            p = sub.add_parser(f"{op}-{kind}")
            p.add_argument("name", nargs="?" if op == "set" else None)
            p.add_argument("--value", default="")
            p.add_argument("--value-file", default="")

    args = ap.parse_args()
    token = get_token()

    if args.action == "list":
        st, sec = call(f"/repos/{REPO}/actions/secrets", token)
        print("Secrets:")
        if st == 200:
            for s in sec.get("secrets", []):
                print(f"  {s['name']:28} 更新于 {s['updated_at']}")
            if not sec.get("secrets"):
                print("  （无）")
        else:
            print(f"  读取失败 {st}: {sec}")
        st, var = call(f"/repos/{REPO}/actions/variables", token)
        print("Variables:")
        if st == 200:
            for v in var.get("variables", []):
                # 变量本身不是敏感值，可以显示
                print(f"  {v['name']:28} = {v['value']}")
            if not var.get("variables"):
                print("  （无）")
        else:
            print(f"  读取失败 {st}: {var}")
        return 0

    op, kind = args.action.split("-", 1)
    name = args.name

    if op in ("set", "delete"):
        # 写操作要求 token 有 admin 权限，先确认
        st, repo = call(f"/repos/{REPO}", token)
        if st == 200 and not repo.get("permissions", {}).get("admin"):
            print("警告：token 可能没有该仓库的 admin 权限，写 secret 会失败")

    if kind == "secret":
        base = f"/repos/{REPO}/actions/secrets"
    else:
        base = f"/repos/{REPO}/actions/variables"

    if op == "get":
        if kind == "var":
            st, res = call(f"{base}/{name}", token)
            print(json.dumps(res, ensure_ascii=False, indent=2) if st == 200 else f"{st} {res}")
        else:
            print("GitHub 不提供读取 secret 明文的接口（这是设计如此）。")
            st, res = call(f"{base}/{name}", token)
            if st == 200:
                print(f"  {name} 存在，更新于 {res.get('updated_at')}")
            else:
                print(f"  {name} 不存在或不可读：{st} {res}")
        return 0

    if op == "delete":
        st, res = call(f"{base}/{name}", token, "DELETE")
        print(f"删除 {kind} {name}: HTTP {st}")
        return 0 if st in (204, 404) else 1

    # set
    value = args.value
    if args.value_file:
        with open(args.value_file, encoding="utf-8") as f:
            value = f.read()
    if not value:
        raise SystemExit("请用 --value 或 --value-file 提供值")

    if kind == "secret":
        st, pk = call(f"/repos/{REPO}/actions/secrets/public-key", token)
        if st != 200:
            print(f"取公钥失败 {st}: {pk}")
            return 1
        sealed = libsodium_seal(pk["key"], value)
        st, res = call(f"{base}/{name}", token, "PUT",
                       {"encrypted_value": sealed, "key_id": pk["key_id"]})
    else:
        st, res = call(base, token, "POST", {"name": name, "value": value})
        if st == 409:      # 已存在 → 改为更新
            st, res = call(f"{base}/{name}", token, "PATCH", {"name": name, "value": value})

    print(f"写入 {kind} {name}: HTTP {st}")
    if st not in (201, 204):
        print(f"  {res}")
        return 1
    print(f"  完成（值长度 {len(value)}，未回显）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
