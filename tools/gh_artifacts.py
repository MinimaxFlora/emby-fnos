#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载 Actions 构建产物（artifact）。

用法::

    python tools/gh_artifacts.py                 # 列出最近一次运行的全部产物
    python tools/gh_artifacts.py --get fpk-x86   # 下载指定产物到 dist/
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402
from gh_logs import api, REPO  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url: str, token: str) -> bytes:
    """artifact 下载同样 302 到 Blob，跟随重定向时必须去掉 Authorization。"""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "emby-fnos-builder"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=120)
        raise SystemExit("预期重定向但没有")
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise SystemExit(f"HTTP {e.code}: {e.read().decode()[:200]}")
        location = e.headers.get("Location")
    req2 = urllib.request.Request(location, headers={"User-Agent": "emby-fnos-builder"})
    with urllib.request.urlopen(req2, timeout=1800) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--get", default="", help="要下载的 artifact 名称")
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    token = get_token()
    st, runs = api(f"/repos/{REPO}/actions/runs?per_page=1", token)
    run = runs["workflow_runs"][0]
    print(f"运行 #{run['run_number']}  {run['status']}/{run.get('conclusion')}")

    st, arts = api(f"/repos/{REPO}/actions/runs/{run['id']}/artifacts", token)
    if st != 200:
        print(f"列产物失败 {st}")
        return 1
    items = arts.get("artifacts", [])
    print(f"产物 {len(items)} 个:")
    for a in items:
        print(f"  {a['name']:16} {a['size_in_bytes'] / 1048576:8.1f} MB  "
              f"已过期={a['expired']}")

    if not args.get:
        return 0

    target = next((a for a in items if a["name"] == args.get), None)
    if not target:
        print(f"没有名为 {args.get} 的产物")
        return 1

    print(f"\n下载 {target['name']} ...")
    blob = download(target["archive_download_url"], token)
    os.makedirs(args.out, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = z.namelist()
        print(f"  压缩包内 {len(names)} 个文件: {names}")
        for n in names:
            dest = os.path.join(args.out, os.path.basename(n))
            with open(dest, "wb") as f:
                f.write(z.read(n))
            print(f"  已写出 {dest}  ({os.path.getsize(dest) / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
