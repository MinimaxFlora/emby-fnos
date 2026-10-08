#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析 Emby 的授权（Premiere）检查逻辑所在位置。

目的：回答「开心版是怎么生效的、是不是靠一个证书文件」。
做法：在未加密（非混淆）的 .NET 程序集元数据里搜相关类型与字段名，
      再把关键字符串周围的字节 dump 出来看上下文。
"""

import os
import re
import sys

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "_work", "image-amd64", "system")
TARGET = os.path.join(BASE, "Emby.Server.Implementations.dll")

# 元数据字符串堆里的标识符
IDENT = re.compile(rb"[A-Za-z_][A-Za-z0-9_.<>+]{3,60}")
KEYWORDS = re.compile(
    rb"licen[cs]|premiere|supporter|trial|regist|MBLicense|mb3admin",
    re.IGNORECASE,
)


def main() -> int:
    if not os.path.isfile(TARGET):
        print(f"找不到 {TARGET}")
        print("（需要先跑一次构建，或改 BASE 指向已导出的镜像树）")
        return 1

    data = open(TARGET, "rb").read()
    print(f"程序集: {TARGET}")
    print(f"大小  : {len(data):,} 字节")

    print("\n=== 授权相关的类型 / 成员 / 字段名 ===")
    seen = set()
    for m in IDENT.finditer(data):
        s = m.group().decode("ascii", "ignore")
        if KEYWORDS.search(m.group()):
            seen.add(s)
    for s in sorted(seen):
        print("  ", s)

    print("\n=== 关键字符串的偏移 ===")
    for key in (b"MBLicenseFile", b"mb3admin", b"SupporterKey",
                b"expDate", b"lastChecked", b"isTrial", b"isValid"):
        offs = []
        start = 0
        while True:
            i = data.find(key, start)
            if i < 0:
                break
            offs.append(i)
            start = i + 1
        print(f"  {key.decode():16} {offs if offs else '（未找到）'}")

    print("\n=== MBLicenseFile 前后字节（看它如何被使用）===")
    i = data.find(b"MBLicenseFile")
    if i >= 0:
        lo, hi = max(0, i - 200), min(len(data), i + 200)
        for j in range(lo, hi, 16):
            chunk = data[j:j + 16]
            txt = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            mark = " <== " if j <= i < j + 16 else ""
            print(f"    {j:8d}  {txt}{mark}")

    print("\n=== 文件名是个 32 位十六进制串，试着反推来源 ===")
    name = "57556c0b1664038946abc87649b9efd8"
    print(f"  文件名: {name}（长度 {len(name)}，纯小写十六进制 → 16 字节）")
    print("  不是 GUID：GUID 是 8-4-4-4-12 带连字符；这个没有连字符")
    print("  看数值像 MD5 / 或两个 64 位整数拼接，属 Emby 内部命名，不必关心含义")
    return 0


if __name__ == "__main__":
    sys.exit(main())
