#!/usr/bin/env bash
# =============================================================================
#  组装 Emby for fnOS 的原生负载（app/ 目录）
#
#  平台中立：CI（Linux）与本机 Windows（经 WSL）都用这一个脚本。
#
#  为什么必须跑在类 Unix 环境
#  --------------------------
#  Debian 用 soname 符号链接组织共享库，Windows 既建不了符号链接
#  （WinError 1314，需管理员/开发者模式），也读不了 WSL 建的链接
#  （WinError 1920，fnpack 报 "The file cannot be accessed by the system"）。
#  所以负载只能在这里组装；Windows 侧只负责最后的 fnpack 打包。
#
#  用法::
#
#      bash build/build_payload.sh x86 [--skip-fetch] [--version 4.10.1.0]
#      bash build/build_payload.sh arm
#      bash build/build_payload.sh all
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORK="${ROOT}/_work"
ASSETS="${ROOT}/app-assets"
PKG="${ROOT}/emby"

VERSION="4.10.1.0"
SKIP_FETCH=0
TARGETS=()

die() { printf '\n\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
ok()  { printf '  \033[32m[OK]\033[0m %s\n' "$*"; }
msg() { printf '  %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        x86|amd64) TARGETS+=("x86") ;;
        arm|arm64) TARGETS+=("arm") ;;
        all)       TARGETS+=("x86" "arm") ;;
        --skip-fetch) SKIP_FETCH=1 ;;
        --version) shift; VERSION="${1:-}" ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        *) die "未知参数：$1" ;;
    esac
    shift
done
[ ${#TARGETS[@]} -gt 0 ] || die "请指定架构：x86 / arm / all"

arch_emby() { case "$1" in x86) echo amd64 ;; arm) echo arm64 ;; esac; }

# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------
echo "==> 检查构建环境"
for c in python3 tar curl; do
    command -v "$c" >/dev/null 2>&1 && ok "$c 可用" || die "缺少 $c"
done
PY=python3

# 符号链接自检：本脚本的全部意义就在于能建符号链接，建不了就别往下走了
_probe="${WORK}/.symlink-probe"
mkdir -p "${WORK}"
rm -f "${_probe}"
if ln -s "target-file" "${_probe}" 2>/dev/null; then
    rm -f "${_probe}"
    ok "符号链接可用（${WORK}）"
else
    die "当前文件系统不支持符号链接（${WORK}），无法组装负载。
      Windows 下请确认工作区在 WSL 的 /mnt/... 路径上，且已启用 DrvFs 的
     symlinkroot 支持；或直接在 Linux 上构建（CI 就是这么做的）。"
fi

# ---------------------------------------------------------------------------
# 逐个架构组装
# ---------------------------------------------------------------------------
for t in "${TARGETS[@]}"; do
    ea="$(arch_emby "$t")"
    app="${PKG}/app"

    printf '\n\033[36m==> 组装 %s 负载（%s，Emby %s）\033[0m\n' "$t" "$ea" "$VERSION"

    deb_dir="${WORK}/deb-${ea}"
    img_dir="${WORK}/image-${ea}"
    deb_file="${WORK}/emby-${ea}.deb"

    # --- 1. 官方 deb -----------------------------------------------------
    if [ "${SKIP_FETCH}" -eq 0 ] || [ ! -d "${deb_dir}/opt/emby-server" ]; then
        if [ ! -f "${deb_file}" ] || [ "${SKIP_FETCH}" -eq 0 ]; then
            url="https://github.com/MediaBrowser/Emby.Releases/releases/download/${VERSION}/emby-server-deb_${VERSION}_${ea}.deb"
            msg "下载官方 deb: ${url}"
            curl -fL --retry 3 -o "${deb_file}" "${url}" \
                || die "官方 deb 下载失败：${url}"
        fi
        msg "解包官方 deb"
        rm -rf "${deb_dir}"
        "${PY}" "${SCRIPT_DIR}/extract_deb.py" --deb "${deb_file}" --out "${deb_dir}" \
            || die "deb 解包失败"
    fi
    [ -d "${deb_dir}/opt/emby-server" ] || die "缺少 ${deb_dir}/opt/emby-server"
    ok "官方 deb 就位：deb-${ea}"

    # --- 2. 镜像树 -------------------------------------------------------
    if [ "${SKIP_FETCH}" -eq 0 ] || [ ! -d "${img_dir}/system" ]; then
        msg "从 registry 导出镜像树（含符号链接）"
        rm -rf "${img_dir}"
        "${PY}" "${SCRIPT_DIR}/export_image_tree.py" \
            --arch "${ea}" --tag "${VERSION}-${ea}" --out "${img_dir}" \
            || die "镜像树导出失败"
    fi
    [ -d "${img_dir}/system" ] || die "缺少 ${img_dir}/system"
    links="$(find "${img_dir}" -type l 2>/dev/null | wc -l)"
    ok "镜像树就位：image-${ea}（含 ${links} 个符号链接）"

    # --- 3. 合并成 app/ --------------------------------------------------
    "${PY}" "${SCRIPT_DIR}/build_native.py" \
        --arch "${ea}" \
        --image-root "${img_dir}" \
        --deb-root "${deb_dir}/opt/emby-server" \
        --out "${app}" \
        --version "${VERSION}" \
        --bundle || die "build_native.py 失败"

    # --- 4. 叠加自有文件 -------------------------------------------------
    cp -a "${ASSETS}/." "${app}/"
    chmod +x "${app}/bin/emby-server" 2>/dev/null || true
    ok "已叠加启动脚本与桌面入口"

    # --- 5. 库目录拆分（真机实测结论）-----------------------------------
    # system/ 给 EmbyServer（.NET 运行时 + Emby 原生依赖）
    # lib/    只留 ffmpeg 的库；随包 libc/loader 必须剔除，否则段错误
    "${PY}" "${SCRIPT_DIR}/split_libs.py" --app-dir "${app}" \
        || die "库目录拆分失败"
    ok "库目录已按真机结论拆分"

    # --- 6. 自检 ---------------------------------------------------------
    stray="$(find "${app}" -type l 2>/dev/null | wc -l)"
    [ "${stray}" -eq 0 ] || die "负载里仍有 ${stray} 个符号链接"
    ok "负载内无符号链接"

    for bad in libc.so.6 ld-linux-x86-64.so.2 ld-linux-aarch64.so.1 \
               libstdc++.so.6 libm.so.6 libgcc_s.so.1; do
        if [ -e "${app}/lib/${bad}" ] || [ -e "${app}/system/${bad}" ]; then
            die "随包 ${bad} 未剔除：它会覆盖系统库并导致段错误"
        fi
    done
    ok "已剔除随包 libc / loader / libstdc++（改用系统库）"

    for f in system/EmbyServer system/libcoreclr.so system/libhostfxr.so \
             system/libsqlite3.so system/libSkiaSharp.so system/libvips.so.42 \
             system/EmbyServer.dll bin/emby-server bin/ffmpeg ui/config; do
        [ -e "${app}/${f}" ] || die "system/ 缺少 ${f}"
    done
    ok "system/ 齐备（.NET 运行时 + Emby 原生依赖）"

    for f in lib/libavcodec.so.59.37.100 lib/libavformat.so.59.27.100 \
             lib/libavutil.so.57.28.100 lib/libswscale.so.6.7.100; do
        [ -e "${app}/${f}" ] || die "lib/ 缺少 ffmpeg 库 ${f}"
    done
    ok "lib/ 保留 ffmpeg 库"

    ok "负载共 $(find "${app}" -type f | wc -l) 个文件"
done

# ---------------------------------------------------------------------------
printf '\n\033[36m==> 负载构建完成\033[0m\n'
printf '    app 目录：%s\n' "${PKG}/app"
printf '    符号链接：%s 个\n' "$(find "${PKG}/app" -type l 2>/dev/null | wc -l)"
printf '\n    接下来打包：\n'
printf '      Linux/CI : fnpack build --directory emby\n'
printf '      Windows  : powershell -ExecutionPolicy Bypass -File build\\build-native.ps1 -SkipFetch\n'
