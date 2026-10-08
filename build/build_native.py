#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组装 Emby for fnOS 的**原生**安装包目录树（不含 Docker）。

数据来源
--------
am（amilys/embyserver 镜像的运行目录，含开心版改动）
    由 export_image_tree.py 导出。这是 Emby 的原生安装树：system/ 里同时放
    EmbyServer 二进制、Emby 程序集**和** .NET 运行时，三者版本自洽，
    必须整块使用 —— 混入官方 deb 的 .NET 会与镜像的 DLL 版本不匹配。

deb（官方 emby-server-deb）
    补三样镜像里缺的：
      etc/fonts    字体配置（镜像里被精简掉了，缺了会影响字幕渲染）
      share        资源文件（libdrm/hwdata 的 ids 等）
      licenses     许可证文本
      lib/dri 里 AMD 的 r600 / radeonsi VAAPI 驱动（LinuxServer 精简掉了）

关于符号链接（本项目最大的坑）
------------------------------
Debian 用 soname 符号链接组织共享库（libstdc++.so.6 -> libstdc++.so.6.0.32），
动态链接器按这些名字找文件。但：

  * Windows 创建符号链接需要管理员权限或开发者模式（WinError 1314）
  * WSL 在 /mnt/d 上创建的链接，Windows 工具读不了
    （fnpack 报 "The file cannot be accessed by the system"）

因此本脚本把链接**解引用成实体文件**写进负载：libstdc++.so.6 会是一份真实的
1.9MB 副本。多占约 40MB，但整条 Windows 打包链都能正常读。

真正的 soname 别名由 build/make_link_manifest.py 生成的 links.tsv 在**安装时**
用 ln -s 重建（见 cmd/common.sh 的 restore_symlinks），运行时行为与上游一致。

用法::

    python build/build_native.py --arch amd64 --image-root _work/image-amd64 \\
        --deb-root _work/deb-amd64/opt/emby-server --out emby/app --version 4.10.1.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys

# 从镜像整块取用的目录（顺序即覆盖顺序，无重叠）
FROM_IMAGE_DIRS = ["system", "bin", "lib"]
# 从官方 deb 取用的目录（镜像里被精简掉或本就缺失）
FROM_DEB_DIRS = ["etc", "share", "licenses"]
# 镜像里的容器专用文件，原生安装必须排除
IMAGE_EXCLUDE = {
    "etc/s6-overlay",
    "etc/passwd",
    "etc/group",
    "etc/shadow",
    "etc/nsswitch.conf",
    "etc/localtime",
    "config",
}
# 额外从 deb 补的 AMD VAAPI 驱动（镜像精简版没有）
DEB_EXTRA_DRI = ["r600_drv_video.so", "radeonsi_drv_video.so"]

# 镜像 bin/ 里为**容器**准备、原生安装完全无用的文件：
#   qemu-*-static  LinuxServer 用 binfmt_misc 做跨架构模拟才需要
#   [ / getconf   Alpine 基础工具，Emby 不依赖
# 留在包里既占体积又扩大攻击面，直接剔除。
IMAGE_BIN_DROP = {
    "qemu-arm-static",
    "qemu-aarch64-static",
    "qemu-i386-static",
    "qemu-x86_64-static",
    "[",
    "getconf",
}


def log(m: str) -> None:
    print(m, flush=True)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def tree_hash(root: str) -> tuple[str, int, int]:
    """对整个目录树做一次稳定摘要，用于产出可复现的指纹。"""
    h = hashlib.sha256()
    total = 0
    count = 0
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(base, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h.update(rel.encode("utf-8"))
            if os.path.islink(full):
                h.update(b"L" + os.readlink(full).encode("utf-8"))
            else:
                st = os.stat(full)
                h.update(b"F" + str(st.st_size).encode())
                if st.st_size and st.st_size < 8 * 1024 * 1024:
                    h.update(sha256_file(full).encode())
            total += os.path.getsize(full) if os.path.exists(full) else 0
            count += 1
    return h.hexdigest(), count, total


def copy_tree(src: str, dst: str, exclude: set[str] | None = None, label: str = "") -> int:
    """复制目录树；符号链接**解引用为实体文件**（原因见模块文档）。"""
    if not os.path.isdir(src):
        log(f"  [warn] 源目录不存在，跳过：{src}")
        return 0
    exclude = exclude or set()
    copied = 0
    deref = 0
    unreadable = 0

    for base, dirs, files in os.walk(src):
        rel_base = os.path.relpath(base, src).replace(os.sep, "/")
        if rel_base == ".":
            rel_base = ""
        # 不跟随链接型目录，避免钻到负载外面；同时应用目录级排除
        dirs[:] = [
            d
            for d in dirs
            if ((rel_base + "/" + d).lstrip("/")) not in exclude
            and not os.path.islink(os.path.join(base, d))
        ]
        target_dir = os.path.join(dst, rel_base.replace("/", os.sep)) if rel_base else dst
        os.makedirs(target_dir, exist_ok=True)

        for fn in files:
            rel = (rel_base + "/" + fn).lstrip("/")
            if rel in exclude:
                continue
            s = os.path.join(base, fn)
            d = os.path.join(target_dir, fn)
            try:
                if os.path.islink(s):
                    real = os.path.realpath(s)
                    if os.path.isfile(real):
                        shutil.copy2(real, d)
                        deref += 1
                    else:
                        unreadable += 1
                        log(f"  [warn] 链接目标不可读（已跳过）：{rel}")
                    continue
                shutil.copy2(s, d)
                copied += 1
            except OSError as exc:
                log(f"  [warn] 复制失败 {rel}: {exc}")

    msg = f"  {label or os.path.basename(src)}: 实体文件 {copied}，链接解引用 {deref}"
    if unreadable:
        msg += f"，不可读 {unreadable}"
    log(msg)
    return copied


def ensure_exec(path: str) -> None:
    try:
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", required=True, choices=["amd64", "arm64"])
    ap.add_argument("--image-root", required=True, help="export_image_tree.py 的输出目录")
    ap.add_argument("--deb-root", required=True, help="官方 deb 解包的 opt/emby-server")
    ap.add_argument("--out", required=True, help="输出目录，一般是 emby/app")
    ap.add_argument("--version", required=True)
    ap.add_argument("--bundle", action="store_true", help="同时写入 provenance-<arch>.json")
    args = ap.parse_args()

    if not os.path.isdir(args.image_root):
        log(f"错误：镜像目录不存在 {args.image_root}")
        log("请先运行：python build/export_image_tree.py --arch ... --tag ... --out ...")
        return 1
    if not os.path.isdir(args.deb_root):
        log(f"错误：官方 deb 目录不存在 {args.deb_root}")
        return 1

    # 输出目录清空重建，避免上次架构残留
    if os.path.isdir(args.out):
        shutil.rmtree(args.out)
    os.makedirs(args.out, exist_ok=True)

    log(f"组装原生包 app/ 目录（{args.arch}，Emby {args.version}）")
    log("")
    log("[1/5] 从镜像取 Emby 运行本体（含 .NET 运行时与开心版改动）")
    for d in FROM_IMAGE_DIRS:
        copy_tree(
            os.path.join(args.image_root, d),
            os.path.join(args.out, d),
            exclude=IMAGE_EXCLUDE,
            label=d,
        )

    log("")
    log("[2/5] 从官方 deb 补齐镜像精简掉的资源")
    for d in FROM_DEB_DIRS:
        copy_tree(os.path.join(args.deb_root, d), os.path.join(args.out, d), label=d)

    log("")
    log("[3/5] 补充 AMD VAAPI 驱动（镜像精简版缺失）")
    dri_dst = os.path.join(args.out, "lib", "dri")
    os.makedirs(dri_dst, exist_ok=True)
    added = 0
    for name in DEB_EXTRA_DRI:
        src = os.path.join(args.deb_root, "lib", "dri", name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dri_dst, name))
            log(f"  + lib/dri/{name} ({os.path.getsize(src) / 1048576:.1f} MB)")
            added += 1
        else:
            log(f"  [warn] 官方 deb 里没有 lib/dri/{name}")
    if not added:
        log("  （无补充）")

    log("")
    log("[4/5] 修正可执行权限并剔除容器专用二进制")
    for rel in ("system/EmbyServer", "bin/ffmpeg", "bin/ffprobe", "bin/ffdetect"):
        p = os.path.join(args.out, rel.replace("/", os.sep))
        if os.path.isfile(p):
            ensure_exec(p)
            log(f"  chmod +x {rel}")
        else:
            log(f"  [warn] 缺少 {rel}")

    dropped = 0
    bin_dir = os.path.join(args.out, "bin")
    if os.path.isdir(bin_dir):
        for name in sorted(os.listdir(bin_dir)):
            if name in IMAGE_BIN_DROP:
                p = os.path.join(bin_dir, name)
                try:
                    size = os.path.getsize(p)
                    os.remove(p)
                    log(f"  - 移除容器专用 bin/{name} ({size / 1048576:.1f} MB)")
                    dropped += 1
                except OSError as exc:
                    log(f"  [warn] 无法移除 bin/{name}: {exc}")
    if not dropped:
        log("  （无可移除项）")

    log("")
    log("[5/5] 结构自检")
    must_have = [
        "system/EmbyServer",
        "system/Emby.Web.dll",
        "system/Emby.Server.Implementations.dll",
        "system/MediaBrowser.Model.dll",
        "system/EmbyServer.runtimeconfig.json",
        "system/libcoreclr.so",
        "system/dashboard-ui/index.html",
        "system/dashboard-ui/ext.js",
        "system/dashboard-ui/embyHappy.js",
        "bin/ffmpeg",
        "lib/libc.so.6",
        "lib/libstdc++.so.6",
    ]
    missing = [r for r in must_have
               if not os.path.exists(os.path.join(args.out, r.replace("/", os.sep)))]
    if missing:
        for r in missing:
            log(f"  [FAIL] 缺少 {r}")
        return 1
    log(f"  必需文件齐备（{len(must_have)} 项）")

    # 负载里不允许出现符号链接：Windows 打包链读不了
    stray_links = []
    for base, dirs, files in os.walk(args.out):
        for fn in files:
            p = os.path.join(base, fn)
            if os.path.islink(p):
                stray_links.append(os.path.relpath(p, args.out).replace(os.sep, "/"))
    if stray_links:
        log(f"  [FAIL] 负载里仍有 {len(stray_links)} 个符号链接（Windows 打包链无法读取）：")
        for r in stray_links[:5]:
            log(f"          {r}")
        return 1
    log("  负载内无符号链接（已全部解引用为实体文件）")

    # 增强注入自检
    idx = os.path.join(args.out, "system", "dashboard-ui", "index.html")
    with open(idx, encoding="utf-8", errors="replace") as f:
        html = f.read()
    if 'data-main="ext"' in html:
        log("  增强模块注入已确认（index.html -> ext.js -> embyHappy/弹幕/播放器增强）")
    else:
        log("  [warn] index.html 未发现 ext.js 注入，界面将为官方原版")

    # 容器残留自检
    leftover = [
        r
        for r in ("etc/s6-overlay", "etc/passwd", "etc/shadow", "config/config/mb.lic")
        if os.path.exists(os.path.join(args.out, r.replace("/", os.sep)))
    ]
    leftover += [
        f"bin/{n}" for n in IMAGE_BIN_DROP
        if os.path.exists(os.path.join(bin_dir, n))
    ]
    if leftover:
        log(f"  [warn] 仍存在容器专用文件：{', '.join(leftover)}")
    else:
        log("  无容器专用文件残留（s6-overlay / passwd / shadow / qemu / [ 均已排除）")

    log("")
    digest, count, total = tree_hash(args.out)
    log(f"产出：{args.out}")
    log(f"  文件数 {count}，总大小 {total / 1048576:.1f} MB")
    log(f"  内容指纹 sha256={digest}")

    if args.bundle:
        prov = {
            "arch": args.arch,
            "emby_version": args.version,
            "payload_files": count,
            "payload_bytes": total,
            "payload_sha256": digest,
            "symlinks_dereferenced": True,
            "note": ("runtime and crack files come from amilys/embyserver image; "
                     "fonts/share/licenses and AMD VAAPI drivers come from the official "
                     "Emby deb; soname symlinks are dereferenced and restored at install "
                     "time from links.tsv"),
        }
        prov_path = os.path.join(
            os.path.dirname(os.path.abspath(args.out)), f"provenance-{args.arch}.json"
        )
        with open(prov_path, "w", encoding="utf-8") as f:
            json.dump(prov, f, indent=2, ensure_ascii=False)
        log(f"  溯源信息已写入 {prov_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
