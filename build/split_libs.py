#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按飞牛真机实测结论拆分负载的库目录。

背景（全部是在真实飞牛 NAS 上实测出来的，不是推测）
----------------------------------------------------
Debian 12 / glibc 2.36 的机器上，镜像自带的那套库**不能整体使用**：

  1. 随包 libc.so.6 是 crosstool-NG 构建的 glibc 2.34，比系统的 2.36 **旧**。
     一旦它进入库搜索路径，整个进程会段错误 —— 实测连 /bin/bash、
     /usr/bin/ldd 都会崩。
  2. 随包的 ld-linux-x86-64.so.2（crosstool-NG）**不读 /etc/ld.so.cache**，
     所以它连系统里的 libavdevice 都找不到。
  3. EmbyServer 的 ELF PT_INTERP 写死 /lib/ld-linux-x86-64.so.2；
     Debian 12 用 multiarch 布局，该路径不存在 → 直接执行报
     “No such file or directory”（看着像文件缺失，其实是没有加载器）。

因此最终布局（本脚本负责产出）：

  system/   EmbyServer + .NET 运行时 + Emby 通过 dlopen 需要的原生库
            （sqlite3 / SkiaSharp / vips / GPU 计算库）
            → 启动方式：系统加载器 --library-path <app>/system
  lib/      只保留 ffmpeg 的库（libav* / libsw* / libpostproc）
            → 启动方式：系统加载器 --library-path <app>/lib
            （ffmpeg 是共享链接的，缺了这些库会报 symbol lookup error）
  lib/dri   VAAPI 驱动，通过 LIBVA_DRIVERS_PATH 使用

被剔除的：libc.so.6 / libm.so.6 / libpthread / libdl / librt / libgcc_s /
libstdc++ / ld-linux-* —— 系统全都有，而且版本更新更匹配。

用法::

    python build/split_libs.py --app-dir emby/app
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

# 随包但**必须剔除**的库：系统已有且版本更新。
# 这些一旦进入搜索路径就会段错误（crosstool-NG glibc 2.34 < 系统 glibc 2.36）。
DROP_PREFIXES = (
    "libc.so", "libc-", "ld-linux", "ld-2.", "libpthread", "libdl.so",
    "librt.so", "libm.so", "libm-", "libgcc_s", "libstdc++",
    "libanl", "libBrokenLocale", "libcrypt", "libnsl", "libresolv",
    "libutil", "libthread_db", "libmvec",
)

# 保留在 lib/ 里给 ffmpeg 用（ffmpeg 是共享链接，缺了会 symbol lookup error）
FFMPEG_KEEP_PREFIXES = (
    "libavcodec", "libavdevice", "libavfilter", "libavformat", "libavutil",
    "libswresample", "libswscale", "libpostproc",
)

# 从 lib/ 搬进 system/ 的库：EmbyServer / .NET 通过 dlopen 需要，
# 而 apphost 的查找目录就是 system/（实测进程映射确认过）。
SYSTEM_TAKE_PREFIXES = (
    "libsqlite3", "libSkiaSharp", "libvips",
    "libigc", "libigdgmm", "libmfx", "libvpl",
    "libdrm", "libva", "libOpenCL",
)


def should_drop(name: str) -> bool:
    return any(name.startswith(p) for p in DROP_PREFIXES)


def is_ffmpeg_lib(name: str) -> bool:
    return any(name.startswith(p) for p in FFMPEG_KEEP_PREFIXES)


def is_system_lib(name: str) -> bool:
    return any(name.startswith(p) for p in SYSTEM_TAKE_PREFIXES)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", required=True)
    args = ap.parse_args()

    app = args.app_dir
    lib = os.path.join(app, "lib")
    system = os.path.join(app, "system")
    if not os.path.isdir(lib) or not os.path.isdir(system):
        print(f"错误：缺少 {lib} 或 {system}")
        return 1

    dropped = kept_ffmpeg = moved = 0
    moved_links = 0

    for name in sorted(os.listdir(lib)):
        src = os.path.join(lib, name)
        if os.path.isdir(src):
            continue  # 保留 lib/dri 等子目录
        if os.path.islink(src):
            # 链接一律丢弃：目标可能已被删，留着就是坏链接；
            # 需要的别名由启动脚本在真机上重建。
            os.remove(src)
            moved_links += 1
            continue
        if should_drop(name):
            os.remove(src)
            dropped += 1
            continue
        if is_ffmpeg_lib(name):
            kept_ffmpeg += 1
            continue
        if is_system_lib(name):
            dst = os.path.join(system, name)
            if not os.path.exists(dst):
                shutil.move(src, dst)
                moved += 1
            else:
                os.remove(src)
                dropped += 1
            continue
        # 其余（libcurl/libcrypto/libiconv 等）系统已有，删掉避免版本冲突
        os.remove(src)
        dropped += 1

    print(f"  lib/ 保留给 ffmpeg：{kept_ffmpeg} 个")
    print(f"  lib/ -> system/ 搬运：{moved} 个")
    print(f"  剔除（系统已有或旧版冲突）：{dropped} 个")
    print(f"  剔除符号链接：{moved_links} 个")

    # 给搬进 system/ 的库补上 soname 别名：Emby 是按 dlopen("libsqlite3.so") 这类
    # 名字找库的，而包里只有 libsqlite3.so.3.51.3 这样的实文件。
    # 用实体副本而不是符号链接：Windows 打包链读不了链接。
    aliases = {
        "libsqlite3": ["libsqlite3.so", "libsqlite3.so.0"],
        "libSkiaSharp": ["libSkiaSharp.so", "libSkiaSharp.so.88"],
        "libvips": ["libvips.so.42"],
        "libigc": ["libigc.so", "libigc.so.1"],
        "libigdgmm": ["libigdgmm.so", "libigdgmm.so.12"],
        "libmfx": ["libmfx.so", "libmfx.so.1"],
        "libvpl": ["libvpl.so", "libvpl.so.2"],
        "libdrm": ["libdrm.so", "libdrm.so.2"],
        "libva": ["libva.so", "libva.so.2"],
        "libOpenCL": ["libOpenCL.so", "libOpenCL.so.1"],
    }
    made = 0
    names = os.listdir(system)
    for prefix, want in aliases.items():
        real = None
        for n in names:
            if n.startswith(prefix) and n != prefix and ".so." in n and not n.endswith(".so"):
                # 取版本号最具体的那份实文件
                if real is None or len(n) > len(real):
                    real = n
        if not real:
            continue
        for alias in want:
            dst = os.path.join(system, alias)
            if os.path.exists(dst):
                continue
            shutil.copy2(os.path.join(system, real), dst)
            made += 1
    print(f"  system/ 补充别名副本：{made} 个")

    # 自检：绝不能有 crosstool 的 libc / loader 残留
    bad = []
    for base, _dirs, files in os.walk(app):
        for f in files:
            if should_drop(f):
                bad.append(os.path.relpath(os.path.join(base, f), app))
    if bad:
        print(f"  [FAIL] 仍存在应剔除的库：{bad[:6]}")
        return 1
    print("  自检通过：无随包 libc / loader 残留")

    # 关键文件在位
    for rel in ("system/EmbyServer", "system/libcoreclr.so",
                "system/libsqlite3.so", "system/libSkiaSharp.so",
                "bin/ffmpeg", "lib/libavcodec.so.59.37.100"):
        if not os.path.exists(os.path.join(app, rel.replace("/", os.sep))):
            print(f"  [FAIL] 缺少 {rel}")
            return 1
    print("  关键文件齐备")

    return 0


if __name__ == "__main__":
    sys.exit(main())
