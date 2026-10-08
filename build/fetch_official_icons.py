#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
获取**官方 Emby 图标**，写入包内图标位置。

为什么单独做这一步
------------------
图标必须是 Emby 官方标识（含透明背景），自己画或从别处转都不对。实测过的坑：

  * 自绘「绿色圆角方块 + 白三角」→ 不是官方标识
  * 用 Emby 的 dashboard `images/icon-512x512.png` → 它虽是 RGBA，但**背景是实心黑**
    （角像素 (0,0,0,255)），生成出来的图标不透明。fnOS 见到不透明图标会套一层
    圆角方块，最终显示成「绿色圆角方块」，与官方外观不一致。
  * Emby Android APK 里的图标同样是黑底的。

**官方 fnOS 应用包里的 ICON.PNG / ICON_256.PNG / ui/images/{64,256}.png 才是正确形态**：
透明背景（alpha 0-255）、品牌绿 #52B54B、斜向边缘的菱形 + 白色播放三角。

本脚本从上游官方 fpk 中提取这四个文件。若本地已有官方图标（`_ref/official-icons/`），
则直接使用，不重新下载。

用法::

    python build/fetch_official_icons.py
    python build/fetch_official_icons.py --from-fpk _ref/official.fpk
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    print("需要 Pillow：pip install Pillow")
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "emby")
ASSETS_IMAGES = os.path.join(ROOT, "app-assets", "ui", "images")
CACHE = os.path.join(ROOT, "_ref", "official-icons")

# 官方 fpk 里的图标 -> 包内目标位置
ICON_MAP = {
    "ICON.PNG": os.path.join(PKG, "ICON.PNG"),
    "ICON_256.PNG": os.path.join(PKG, "ICON_256.PNG"),
    "ui/images/64.png": os.path.join(ASSETS_IMAGES, "64.png"),
    "ui/images/256.png": os.path.join(ASSETS_IMAGES, "256.png"),
}

UPSTREAM_FPK = (
    "https://github.com/conversun/fnos-apps/releases/download/"
    "emby%2Fv4.10.1.0/embyserver_4.10.1.0_x86.fpk"
)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_icon(path: str) -> tuple[bool, str]:
    """校验图标：必须是正方形 PNG 且**带透明背景**。"""
    try:
        im = Image.open(path)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法打开：{exc}"
    if im.size[0] != im.size[1]:
        return False, f"非正方形：{im.size}"
    rgba = im.convert("RGBA")
    lo, _hi = rgba.getchannel("A").getextrema()
    if lo > 8:
        return False, f"背景不透明（alpha 最小 {lo}）—— fnOS 会套圆角方块，外观不对"
    corner = rgba.getpixel((0, 0))
    if corner[3] > 8:
        return False, f"左上角不是透明：{corner}"
    return True, f"{im.size[0]}x{im.size[1]} alpha 0-255 透明背景 OK"


def extract_from_fpk(fpk: str) -> int:
    if not os.path.isfile(fpk):
        print(f"错误：找不到 {fpk}")
        return 1
    os.makedirs(CACHE, exist_ok=True)
    got = 0
    with tarfile.open(fpk, "r:*") as tf:
        names = set(tf.getnames())
        for member, _dest in ICON_MAP.items():
            if member not in names:
                print(f"  [warn] 官方包里没有 {member}")
                continue
            data = tf.extractfile(member).read()
            out = os.path.join(CACHE, member.replace("/", os.sep))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
            got += 1
    print(f"  已从官方 fpk 提取 {got} 个图标 -> {os.path.relpath(CACHE, ROOT)}")
    return 0 if got == len(ICON_MAP) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-fpk", default="", help="官方 fpk 路径（默认用 _ref/official.fpk 或下载）")
    ap.add_argument("--check", action="store_true", help="只校验包内现有图标")
    args = ap.parse_args()

    if args.check:
        print("包内图标校验（必须透明背景）：")
        bad = 0
        for _member, dest in ICON_MAP.items():
            rel = os.path.relpath(dest, ROOT)
            if not os.path.isfile(dest):
                print(f"  [缺] {rel}")
                bad += 1
                continue
            ok, msg = check_icon(dest)
            print(f"  {'[OK]' if ok else '[FAIL]'} {rel}  {msg}")
            if not ok:
                bad += 1
        print(f"\n结论：{'全部合格' if bad == 0 else str(bad) + ' 项不合格'}")
        return 0 if bad == 0 else 1

    # 1. 优先用缓存
    cached = all(os.path.isfile(os.path.join(CACHE, m.replace("/", os.sep))) for m in ICON_MAP)
    if not cached:
        fpk = args.from_fpk or os.path.join(ROOT, "_ref", "official.fpk")
        if not os.path.isfile(fpk):
            print(f"本地没有官方 fpk，正在下载：\n  {UPSTREAM_FPK}")
            import urllib.request

            os.makedirs(os.path.dirname(fpk), exist_ok=True)
            req = urllib.request.Request(UPSTREAM_FPK, headers={"User-Agent": "emby-fpk/1.0"})
            with urllib.request.urlopen(req, timeout=1800) as r, open(fpk, "wb") as f:
                shutil.copyfileobj(r, f)
            print(f"  下载完成（{os.path.getsize(fpk) / 1048576:.1f} MB）")
        if extract_from_fpk(fpk) != 0:
            return 1
    else:
        print(f"使用缓存的官方图标：{os.path.relpath(CACHE, ROOT)}")

    # 2. 复制到包内位置并校验
    print("\n写入包内图标：")
    bad = 0
    for member, dest in ICON_MAP.items():
        src = os.path.join(CACHE, member.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        ok, msg = check_icon(dest)
        rel = os.path.relpath(dest, ROOT)
        print(f"  {'[OK]' if ok else '[FAIL]'} {rel:34} {os.path.getsize(dest):6d} B  {msg}")
        if not ok:
            bad += 1
        else:
            print(f"       sha256={sha256(dest)}")

    if bad:
        print(f"\n[FAIL] {bad} 个图标不合格")
        return 1
    print("\n完成：图标均为 Emby 官方标识，带透明背景。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
