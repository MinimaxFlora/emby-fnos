#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解包官方 Emby .deb（ar 归档 + data.tar.xz），只保留 opt/emby-server 下的程序文件。

为什么要自己解：
    .deb 是 ar 归档，Windows 上没有 ar 命令；本机也没有 dpkg。
    用标准库 ar 解析 + lzma 解压即可，跨平台且无外部依赖。

被 build-native.ps1 调用，产物作为「官方基准」：
    etc/fonts, share, licenses   —— 镜像里被精简掉的资源
    lib/dri/r600|radeonsi_drv_video.so —— 镜像里被精简掉的 AMD VAAPI 驱动

关于扩展名：文件名带下划线（extract_deb.py）是刻意的 —— 它会被当作模块导入，
同时也能直接执行；若叫 extract-deb.py 则无法 import。

用法::

    # 命令行
    python build/extract_deb.py --deb _work/emby-amd64.deb --out _work/deb-amd64

    # 作为模块
    from extract_deb import extract_deb
    extract_deb("emby.deb", "deb-amd64", log=print)
"""

from __future__ import annotations

import argparse
import gzip
import io
import lzma
import os
import sys
import tarfile

# 只保留 Emby 程序本体，跳过文档/桌面快捷方式/图标等无关内容
KEEP_PREFIX = "./opt/emby-server/"


def ar_members(path: str):
    """生成 ar 归档成员 (name, size, data_offset)。

    ar 头是 60 字节定长文本；数据按 2 字节对齐（奇数长度补一个 '\\n'）。
    """
    with open(path, "rb") as f:
        magic = f.read(8)
        if magic != b"!<arch>\n":
            raise RuntimeError(f"不是 ar 归档（deb 文件头应为 !<arch>）：{magic!r}")
        while True:
            hdr = f.read(60)
            if len(hdr) < 60:
                return
            name = hdr[0:16].decode("ascii", "replace").strip()
            try:
                size = int(hdr[48:58].decode("ascii").strip())
            except ValueError as exc:
                raise RuntimeError(f"ar 成员长度字段损坏：{hdr[48:58]!r}") from exc
            offset = f.tell()
            yield name.rstrip("/"), size, offset
            f.seek(offset + size + (size & 1))


def extract_deb(deb: str, out_dir: str, log=print) -> int:
    """解出 data.tar.* 中的 opt/emby-server 到 out_dir，返回文件数。"""
    os.makedirs(out_dir, exist_ok=True)

    target = None
    for name, size, offset in ar_members(deb):
        log(f"  ar 成员 {name}  {size / 1048576:.1f} MB")
        if name.startswith("data.tar"):
            target = (name, size, offset)
    if target is None:
        raise RuntimeError("deb 里没有 data.tar.* 成员")

    name, size, offset = target
    log(f"  解包 {name} ...")
    with open(deb, "rb") as f:
        f.seek(offset)
        raw = f.read(size)

    if name.endswith(".xz"):
        raw = lzma.decompress(raw)
    elif name.endswith(".gz"):
        raw = gzip.decompress(raw)
    elif name.endswith(".zst"):
        raise RuntimeError("data.tar.zst 需要 zstandard 模块，请改用 .xz 版本的 deb")

    count = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        for m in tf:
            if not (m.isfile() or m.issym() or m.islnk()):
                continue
            if not m.name.startswith(KEEP_PREFIX):
                continue
            m.name = m.name[2:]  # 去掉开头的 "./"
            try:
                tf.extract(m, out_dir, filter="data")
                count += 1
            except Exception as exc:  # noqa: BLE001
                log(f"    [warn] 跳过 {m.name}: {exc}")

    log(f"  已解出 {count} 个文件到 {out_dir}")
    root = os.path.join(out_dir, "opt", "emby-server")
    if not os.path.isdir(root):
        raise RuntimeError(f"解包结果缺少 {root}，deb 结构可能已变化")
    return count


def main() -> int:
    ap = argparse.ArgumentParser(description="解包官方 Emby .deb")
    ap.add_argument("--deb", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if not os.path.isfile(args.deb):
        print(f"错误：找不到 {args.deb}")
        return 1
    extract_deb(args.deb, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
