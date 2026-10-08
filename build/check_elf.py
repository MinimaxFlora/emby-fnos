#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
静态检查 Emby 原生负载的 ELF 兼容性（无需在目标机上运行）。

检查项
------
1. **PT_INTERP**：可执行文件写死的动态加载器路径。这是启动失败最常见的原因 ——
   程序启动时内核按这个**绝对路径**去找加载器，LD_LIBRARY_PATH 对它无效。
   如果目标系统没有这个路径，就会直接 `No such file or directory`。
2. **NEEDED**：依赖的共享库，逐个确认随包 lib/ 里有没有。
3. **GLIBC 符号版本**：程序引用的最高 GLIBC_x.y，与随包 libc 提供的版本对比。

用法::

    python build/check_elf.py --app-dir emby/app --arch amd64
"""

from __future__ import annotations

import argparse
import os
import re
import struct
import sys

PT_INTERP = 3
PT_LOAD = 1
DT_NEEDED = 1


def read_elf_header(data: bytes) -> dict:
    if data[:4] != b"\x7fELF":
        raise ValueError("非 ELF 文件")
    is64 = data[4] == 2
    little = data[5] == 1
    endian = "<" if little else ">"
    machine = struct.unpack_from(endian + "H", data, 18)[0]
    if is64:
        e_phoff = struct.unpack_from(endian + "Q", data, 32)[0]
        e_phentsize = struct.unpack_from(endian + "H", data, 54)[0]
        e_phnum = struct.unpack_from(endian + "H", data, 56)[0]
    else:
        e_phoff = struct.unpack_from(endian + "I", data, 28)[0]
        e_phentsize = struct.unpack_from(endian + "H", data, 42)[0]
        e_phnum = struct.unpack_from(endian + "H", data, 44)[0]
    return {
        "is64": is64,
        "endian": endian,
        "machine": machine,
        "phoff": e_phoff,
        "phentsize": e_phentsize,
        "phnum": e_phnum,
    }


def program_interp(data: bytes, hdr: dict) -> str | None:
    endian = hdr["endian"]
    for i in range(hdr["phnum"]):
        off = hdr["phoff"] + i * hdr["phentsize"]
        p_type = struct.unpack_from(endian + "I", data, off)[0]
        if p_type != PT_INTERP:
            continue
        if hdr["is64"]:
            p_offset = struct.unpack_from(endian + "Q", data, off + 8)[0]
            p_filesz = struct.unpack_from(endian + "Q", data, off + 32)[0]
        else:
            p_offset = struct.unpack_from(endian + "I", data, off + 4)[0]
            p_filesz = struct.unpack_from(endian + "I", data, off + 16)[0]
        raw = data[p_offset : p_offset + p_filesz]
        return raw.split(b"\x00", 1)[0].decode("ascii", "replace")
    return None


def find_dynamic(data: bytes, hdr: dict) -> tuple[int, int] | None:
    """返回 (vaddr, filesz) of PT_DYNAMIC。"""
    endian = hdr["endian"]
    for i in range(hdr["phnum"]):
        off = hdr["phoff"] + i * hdr["phentsize"]
        p_type = struct.unpack_from(endian + "I", data, off)[0]
        if p_type != 2:  # PT_DYNAMIC
            continue
        if hdr["is64"]:
            p_vaddr = struct.unpack_from(endian + "Q", data, off + 16)[0]
            p_filesz = struct.unpack_from(endian + "Q", data, off + 32)[0]
        else:
            p_vaddr = struct.unpack_from(endian + "I", data, off + 8)[0]
            p_filesz = struct.unpack_from(endian + "I", data, off + 16)[0]
        return p_vaddr, p_filesz
    return None


def vaddr_to_offset(data: bytes, hdr: dict, vaddr: int) -> int | None:
    endian = hdr["endian"]
    for i in range(hdr["phnum"]):
        off = hdr["phoff"] + i * hdr["phentsize"]
        p_type = struct.unpack_from(endian + "I", data, off)[0]
        if p_type != PT_LOAD:
            continue
        if hdr["is64"]:
            p_offset = struct.unpack_from(endian + "Q", data, off + 8)[0]
            p_vaddr = struct.unpack_from(endian + "Q", data, off + 16)[0]
            p_filesz = struct.unpack_from(endian + "Q", data, off + 32)[0]
        else:
            p_offset = struct.unpack_from(endian + "I", data, off + 4)[0]
            p_vaddr = struct.unpack_from(endian + "I", data, off + 8)[0]
            p_filesz = struct.unpack_from(endian + "I", data, off + 16)[0]
        if p_vaddr <= vaddr < p_vaddr + p_filesz:
            return p_offset + (vaddr - p_vaddr)
    return None


def dynamic_needed(data: bytes, hdr: dict) -> list[str]:
    dyn = find_dynamic(data, hdr)
    if not dyn:
        return []
    vaddr, filesz = dyn
    base = vaddr_to_offset(data, hdr, vaddr)
    if base is None:
        return []
    endian = hdr["endian"]
    entsize = 16 if hdr["is64"] else 8
    strtab_addr = None
    needed_offsets = []
    off = base
    limit = base + filesz
    while off < limit:
        if hdr["is64"]:
            tag = struct.unpack_from(endian + "q", data, off)[0]
            val = struct.unpack_from(endian + "Q", data, off + 8)[0]
        else:
            tag = struct.unpack_from(endian + "i", data, off)[0]
            val = struct.unpack_from(endian + "I", data, off + 4)[0]
        if tag == 0:
            break
        if tag == DT_NEEDED:
            needed_offsets.append(val)
        elif tag == 5:  # DT_STRTAB
            strtab_addr = val
        off += entsize

    if strtab_addr is None:
        return []
    strtab_off = vaddr_to_offset(data, hdr, strtab_addr)
    if strtab_off is None:
        return []
    out = []
    for no in needed_offsets:
        start = strtab_off + no
        end = data.find(b"\x00", start)
        out.append(data[start:end].decode("ascii", "replace"))
    return out


def glibc_versions(data: bytes, limit: int = 400) -> list[str]:
    """扫描 GLIBC_x.y 版本字符串。

    用正则一次过 —— 早先的逐字节版本在 1GB 的 libcoreclr.so 上会跑几十秒。
    """
    found = set()
    for m in re.finditer(rb"GLIBC_2\.\d+(?:\.\d+)?", data):
        found.add(m.group(0).decode("ascii"))
        if len(found) >= limit:
            break
    def key(s: str):
        return [int(x) for x in s.split("_")[1].split(".")]
    return sorted(found, key=key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", required=True)
    args = ap.parse_args()

    app = args.app_dir
    binpath = os.path.join(app, "system", "EmbyServer")
    libdir = os.path.join(app, "lib")

    if not os.path.isfile(binpath):
        print(f"错误：找不到 {binpath}")
        return 1

    with open(binpath, "rb") as f:
        data = f.read()
    hdr = read_elf_header(data)
    arch = {0x3E: "x86-64", 0xB7: "aarch64"}.get(hdr["machine"], hex(hdr["machine"]))
    print(f"检查 {os.path.relpath(binpath, app)}  ({arch})")

    interp = program_interp(data, hdr)
    print(f"\n[1] PT_INTERP（内核按此绝对路径找加载器，LD_LIBRARY_PATH 无效）")
    print(f"    {interp}")
    bundled_loader_name = "ld-linux-x86-64.so.2" if hdr["machine"] == 0x3E else "ld-linux-aarch64.so.1"
    bundled_loader = os.path.join(libdir, bundled_loader_name)
    print(f"    随包加载器：{bundled_loader_name} -> {'存在' if os.path.isfile(bundled_loader) else '缺失'}")
    print(f"    ⚠ 目标系统若没有 {interp}，程序会直接报 'No such file or directory'；")
    print(f"      启动脚本必须能回退到随包加载器：{bundled_loader_name}")

    needed = dynamic_needed(data, hdr)
    print(f"\n[2] NEEDED（{len(needed)} 个）")
    missing = []
    for n in needed:
        p = os.path.join(libdir, n)
        ok = os.path.isfile(p)
        if not ok:
            missing.append(n)
        print(f"    {'OK ' if ok else '缺 '} {n}")
    if missing:
        print(f"    ⚠ 随包 lib/ 缺少：{', '.join(missing)}（系统若有也能跑）")

    print(f"\n[3] GLIBC 符号版本")
    req = glibc_versions(data)
    print(f"    EmbyServer 引用：{', '.join(req[-6:]) if req else '(未发现)'}")
    libc = os.path.join(libdir, "libc.so.6")
    if os.path.isfile(libc):
        with open(libc, "rb") as f:
            ldata = f.read()
        prov = glibc_versions(ldata)
        print(f"    随包 libc 提供：{', '.join(prov[-6:]) if prov else '(未发现)'}")

    print("\n[4] 随包关键库清点")
    for n in ("libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
              "libstdc++.so.6", "libgcc_s.so.1", bundled_loader_name):
        p = os.path.join(libdir, n)
        size = os.path.getsize(p) if os.path.isfile(p) else 0
        print(f"    {'OK ' if size else '缺 '} {n:<26} {size / 1024:8.0f} KB")

    return 0


if __name__ == "__main__":
    sys.exit(main())
