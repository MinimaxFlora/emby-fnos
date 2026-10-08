#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
查看 GitHub Actions 运行状态与日志（只读）。

用法::

    python tools/gh_logs.py                 # 最近一次运行的状态摘要
    python tools/gh_logs.py --jobs          # 每个 job 与步骤的状态
    python tools/gh_logs.py --log           # 拉失败步骤的日志尾部
    python tools/gh_logs.py --watch         # 轮询直到结束
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402

REPO = os.environ.get("EMBY_GH_REPO_FULL", "MinimaxFlora/emby-fnos")


def api(path: str, token: str, raw: bool = False):
    req = urllib.request.Request(
        path if path.startswith("http") else f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "emby-fnos-builder"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
            return r.status, (data if raw else json.loads(data.decode() or "{}"))
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def latest_run(token: str) -> dict | None:
    st, runs = api(f"/repos/{REPO}/actions/runs?per_page=1", token)
    if st != 200 or not runs.get("workflow_runs"):
        return None
    return runs["workflow_runs"][0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    token = get_token()

    while True:
        run = latest_run(token)
        if not run:
            print("没有运行记录")
            return 1
        print(f"运行 #{run['run_number']}  {run['name']}")
        print(f"  状态: {run['status']} / {run.get('conclusion') or '-'}")
        print(f"  分支: {run['head_branch']}  commit {run['head_sha'][:8]}")
        print(f"  链接: {run['html_url']}")

        st, jobs = api(f"/repos/{REPO}/actions/runs/{run['id']}/jobs", token)
        if st == 200:
            for j in jobs.get("jobs", []):
                mark = {"success": "OK", "failure": "FAIL", "skipped": "SKIP",
                        "cancelled": "CANCEL"}.get(j.get("conclusion") or "", "..")
                print(f"  [{mark}] {j['name']}  ({j['status']})")
                if args.jobs or args.log:
                    for s in j.get("steps", []):
                        sm = {"success": "OK", "failure": "FAIL",
                              "skipped": "SKIP"}.get(s.get("conclusion") or "", "..")
                        print(f"        [{sm}] {s['name']}")

        if run["status"] == "completed":
            if args.log:
                failed = []
                if st == 200:
                    for j in jobs.get("jobs", []):
                        if j.get("conclusion") == "failure":
                            for s in j.get("steps", []):
                                if s.get("conclusion") == "failure":
                                    failed.append((j["id"], j["name"], s["name"]))
                for jid, jname, sname in failed[:2]:
                    print(f"\n===== 失败步骤日志：{jname} / {sname} =====")
                    st2, blob = api(
                        f"/repos/{REPO}/actions/jobs/{jid}/logs", token, raw=True)
                    if st2 == 200:
                        text = blob.decode("utf-8", "replace")
                        lines = text.splitlines()
                        print("\n".join(lines[-60:]))
                    else:
                        print(f"  取日志失败 {st2}")
            return 0 if run.get("conclusion") == "success" else 1

        if not args.watch:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
