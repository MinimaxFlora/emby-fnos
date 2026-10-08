#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 amilys/embyserver 镜像里的 emby 运行目录完整导出到本地，用于离线分析。

导出策略：按层顺序解包，后层覆盖前层，最终结果就是容器里真实看到的文件系统
（只保留 emby 相关子树，跳过 /usr、/etc/ssl 等无关目录）。

用法::

    python build/export_image_tree.py --arch amd64 --tag 4.10.1.0-amd64 \
        --out _work/image-amd64
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
import urllib.request

REGISTRY = "https://registry-1.docker.io"
AUTH_URL = "https://auth.docker.io/token"
SERVICE = "registry.docker.io"
ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
CHUNK = 1 << 20

# 只导出这些顶层目录（emby 程序本体），避免把整个 LinuxServer 根文件系统拉下来
WANTED_TOP = {"system", "bin", "etc", "lib", "share", "licenses", "extra", "config"}


def log(m: str) -> None:
    print(m, flush=True)


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "fnos-emby-export/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def manifest(repo: str, ref: str, token: str) -> dict:
    req = urllib.request.Request(f"{REGISTRY}/v2/{repo}/manifests/{ref}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", ACCEPT)
    req.add_header("User-Agent", "fnos-emby-export/1.0")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="amilys/embyserver")
    ap.add_argument("--arch", required=True, choices=["amd64", "arm64"])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--all", action="store_true", help="导出全部路径（默认只导 emby 子树）")
    args = ap.parse_args()

    token = http_json(f"{AUTH_URL}?service={SERVICE}&scope=repository:{args.repo}:pull")["token"]
    idx = manifest(args.repo, args.tag, token)
    sub = [
        m
        for m in idx["manifests"]
        if m.get("platform", {}).get("architecture") == args.arch
        and "attestation" not in m.get("annotations", {}).get("vnd.docker.reference.type", "")
    ][0]
    layers = manifest(args.repo, sub["digest"], token)["layers"]

    os.makedirs(args.out, exist_ok=True)
    written = 0
    skipped = 0

    # 符号链接必须单独收集，最后统一创建。
    # 原因：镜像层里经常是「先有 libfoo.so.1 -> libfoo.so.1.2.3 的链，后才有实体」，
    # 边扫边建会因目标尚不存在而失败；而且链接目标本身也可能是另一条链。
    # 早先的版本直接跳过了符号链接，导致动态链接器找不到 libstdc++.so.6 这类
    # 「soname 别名」，Emby 根本起不来 —— 这是必须修的关键 bug。
    pending_links: dict[str, str] = {}

    for li, layer in enumerate(layers):
        url = f"{REGISTRY}/v2/{args.repo}/blobs/{layer['digest']}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
        log(f"  层 {li + 1}/{len(layers)}  {layer['digest'][7:19]}  {len(raw) / 1048576:.1f} MB")

        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as tf:
            for m in tf:
                p = m.name.lstrip("./")
                if not p:
                    continue
                top = p.split("/", 1)[0]
                if not args.all and top not in WANTED_TOP:
                    skipped += 1
                    continue

                if m.isdir():
                    os.makedirs(os.path.join(args.out, p.replace("/", os.sep)), exist_ok=True)
                    continue

                # 硬链接：镜像里少见，按普通文件内容复制即可（tarfile 会跟随）
                if m.issym() or m.islnk():
                    pending_links[p] = m.linkname
                    continue

                # 白化文件：表示删除
                if os.path.basename(p).startswith(".wh."):
                    target = os.path.join(
                        args.out, os.path.dirname(p).replace("/", os.sep)
                    )
                    victim = os.path.join(target, os.path.basename(p)[4:])
                    if os.path.exists(victim):
                        os.remove(victim)
                    pending_links.pop(
                        os.path.relpath(victim, args.out).replace(os.sep, "/"), None
                    )
                    continue

                if not m.isfile():
                    continue

                dest = os.path.join(args.out, p.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)

                with open(dest, "wb") as f:
                    src = tf.extractfile(m)
                    while True:
                        b = src.read(CHUNK)
                        if not b:
                            break
                        f.write(b)
                try:
                    os.chmod(dest, m.mode & 0o7777)
                except OSError:
                    pass
                written += 1

    # ------------------------------------------------------------------
    # 物化符号链接：多轮处理，直到没有进展（处理链式链接）
    # ------------------------------------------------------------------
    links_made = 0
    links_broken: list[tuple[str, str]] = []
    remaining = dict(pending_links)

    for _round in range(8):
        if not remaining:
            break
        progressed = False
        for rel, target in list(remaining.items()):
            dest = os.path.join(args.out, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest) or args.out, exist_ok=True)

            # 链接目标可能是绝对路径（容器内路径）或相对同目录的名称
            if target.startswith("/"):
                resolved = os.path.join(args.out, target.lstrip("/").replace("/", os.sep))
            else:
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(dest), target.replace("/", os.sep))
                )

            if not os.path.exists(resolved):
                continue  # 目标还没建好，下一轮再试
            if os.path.lexists(dest):
                os.remove(dest)
            try:
                os.symlink(target, dest)
                links_made += 1
                del remaining[rel]
                progressed = True
            except OSError as exc:
                log(f"    [warn] 建符号链接失败 {rel} -> {target}: {exc}")
                del remaining[rel]
        if not progressed:
            break

    links_broken = sorted(remaining.items())

    log("")
    log(f"导出完成：{written} 个文件，{links_made} 个符号链接 -> {args.out}（跳过 {skipped} 个无关条目）")
    if links_broken:
        log(f"  [warn] {len(links_broken)} 个符号链接的目标不在导出范围内（可能是容器专用路径）：")
        for rel, target in links_broken[:10]:
            log(f"          {rel} -> {target}")
    log("顶层内容：")
    for e in sorted(os.listdir(args.out)):
        full = os.path.join(args.out, e)
        kind = "dir " if os.path.isdir(full) else ("link" if os.path.islink(full) else "file")
        log(f"    {kind} {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
