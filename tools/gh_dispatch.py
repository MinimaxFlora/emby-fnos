#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""手动触发工作流并等待结果（用于验证发布行为）。"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from push_to_github import get_token  # noqa: E402
from gh_logs import api, REPO  # noqa: E402
from gh_secrets import call  # noqa: E402  （gh_logs.api 只支持 GET，这里要 POST）


def main() -> int:
    token = get_token()
    workflow = "build.yml"
    st, res = call(f"/repos/{REPO}/actions/workflows/{workflow}/dispatches",
                   token, "POST", {"ref": "main"})
    print(f"触发工作流: HTTP {st}")
    if st not in (204, 201):
        print(f"  {res}")
        return 1

    # 等新的一轮出现
    time.sleep(10)
    for _ in range(60):
        st, runs = api(f"/repos/{REPO}/actions/runs?per_page=1", token)
        run = runs["workflow_runs"][0]
        print(f"  run #{run['run_number']}  {run['status']}/{run.get('conclusion') or '-'}")
        if run["status"] == "completed":
            break
        time.sleep(30)
    return 0 if run.get("conclusion") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
