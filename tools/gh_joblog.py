#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉取 Actions 作业日志（跟随重定向；token 不落盘）。"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402
from gh_logs import api, REPO  # noqa: E402


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_log(job_id: int, token: str) -> str:
    """日志接口会 302 到 Azure Blob 存储。

    关键：**跟随重定向时不能带 Authorization 头** —— Azure 会对带签名的 URL
    再叠加 Authorization 判为签名不匹配，返回 401 InvalidAuthenticationInfo。
    所以先不跟随重定向拿到 Location，再裸请求那个 URL。
    """
    api_url = f"https://api.github.com/repos/{REPO}/actions/jobs/{job_id}/logs"
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "emby-fnos-builder"})
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(req, timeout=120)
        return "__NO_REDIRECT__"
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            return f"__HTTP_{e.code}__ {e.read().decode()[:200]}"
        location = e.headers.get("Location")
    if not location:
        return "__NO_LOCATION__"

    req2 = urllib.request.Request(location, headers={
        "User-Agent": "emby-fnos-builder"})   # 刻意不带 Authorization
    try:
        with urllib.request.urlopen(req2, timeout=180) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return f"__BLOB_HTTP_{e.code}__ {e.read().decode()[:200]}"


def main() -> int:
    token = get_token()
    st, runs = api(f"/repos/{REPO}/actions/runs?per_page=1", token)
    run = runs["workflow_runs"][0]
    print(f"运行 #{run['run_number']}  {run['status']}/{run.get('conclusion')}")
    st, jobs = api(f"/repos/{REPO}/actions/runs/{run['id']}/jobs", token)
    for j in jobs["jobs"]:
        if j.get("conclusion") != "failure":
            continue
        print(f"\n########## {j['name']} ##########")
        text = fetch_log(j["id"], token)
        if text.startswith("__HTTP_"):
            print("  " + text[:200])
            continue
        lines = text.splitlines()
        # 只打印与测试相关的行，以及失败标记附近
        keep = []
        for i, ln in enumerate(lines):
            s = ln.strip()
            if ("FAIL" in s or "结果：" in s or "not found" in s.lower()
                    or "error" in s.lower() and "erroraction" not in s.lower()):
                keep.append((i, s))
        for i, s in keep[-40:]:
            print(f"  {s[:180]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
