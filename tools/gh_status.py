#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 GitHub 仓库的文件列表与工作流状态（只读）。"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402


def api(path: str, token: str):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "emby-fnos-builder"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"message": e.read().decode()[:300]}


def main() -> int:
    repo = os.environ.get("EMBY_GH_REPO_FULL", "MinimaxFlora/emby-fnos")
    token = get_token()

    st, info = api(f"/repos/{repo}", token)
    if st != 200:
        print(f"读取仓库失败 {st}: {info}")
        return 1
    print(f"仓库: {info['full_name']}")
    print(f"  分支: {info['default_branch']}  私有: {info['private']}")
    print(f"  URL : {info['html_url']}")

    st, tree = api(f"/repos/{repo}/git/trees/{info['default_branch']}?recursive=1", token)
    if st != 200:
        print(f"读取文件树失败 {st}")
        return 1
    blobs = [t for t in tree.get("tree", []) if t["type"] == "blob"]
    print(f"\n文件共 {len(blobs)} 个:")
    for t in sorted(blobs, key=lambda x: x["path"]):
        print(f"  {t['size']:>9,} B  {t['path']}")

    st, wf = api(f"/repos/{repo}/actions/workflows", token)
    if st == 200:
        print(f"\nActions 工作流 {wf.get('total_count', 0)} 个:")
        for w in wf.get("workflows", []):
            print(f"  {w['name']}  [{w['state']}]  {w['path']}")
    st, runs = api(f"/repos/{repo}/actions/runs?per_page=5", token)
    if st == 200:
        print(f"\n最近运行 {runs.get('total_count', 0)} 次:")
        for r in runs.get("workflow_runs", [])[:5]:
            print(f"  #{r['run_number']}  {r['name']}  {r['status']}/{r['conclusion']}  "
                  f"{r['head_branch']}  {r['html_url']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
