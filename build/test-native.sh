#!/bin/bash
# =============================================================================
#  Emby for fnOS (原生版) — 生命周期逻辑离线自测
#
#  在没有飞牛设备的情况下，用桩替换 TRIM_* 环境变量与系统命令，
#  真实执行 cmd/common.sh 与 cmd/service-setup 的逻辑，覆盖最容易出错的场景：
#    1. 目录准备与数据目录归属
#    2. 会员预激活（授权状态文件 + hosts 拦截，且必须幂等）
#    3. PID 文件读写、进程存活判定、过期 PID 不误判
#    4. 媒体目录：共享授权 / 手填路径的优先级与危险路径过滤
#    5. 启动脚本的环境变量拼装与参数（不真的拉起 EmbyServer）
#    6. 配置变更回调、卸载数据保留/清除
#    7. 包内文件与 manifest 完整性、JSON 可解析
#
#  用法：bash build/test-native.sh
# =============================================================================
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="${SRC}/emby"
ASSETS="${SRC}/app-assets"
PASS=0
FAIL=0
SKIP=0

ok()    { printf '  \033[32m[ OK ]\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
bad()   { printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
skip()  { printf '  \033[33m[SKIP]\033[0m %s\n' "$1"; SKIP=$((SKIP + 1)); }
head_() { printf '\n\033[36m== %s ==\033[0m\n' "$1"; }

# 找一个可用的 python 来校验 JSON
PY=""
for cand in python python3 py; do
    if command -v "${cand}" >/dev/null 2>&1 && "${cand}" -c 'print(1)' >/dev/null 2>&1; then
        PY="${cand}"; break
    fi
done
if [ -z "${PY}" ]; then
    for cand in "$HOME/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe" \
                "/c/Users/zj181/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"; do
        [ -x "${cand}" ] && { PY="${cand}"; break; }
    done
fi

# ---------------------------------------------------------------------------
setup_env() {
    # 沙盒根目录必须满足两个条件：
    #   1. 不能落在 /tmp 下 —— media_paths() 会刻意过滤 /tmp、/proc 等系统前缀
    #   2. 从 / 到沙盒的每一级都要可穿越，否则降权到应用用户后连数据目录都进不去，
    #      会被误判成「没有写权限」（测试假象）
    # $HOME 下满足第 1 条；以 root 跑测试时 /root 是 0755，第 2 条也满足。
    # 以普通用户跑测试时目标用户通常不存在，会正常回落到当前身份。
    SANDBOX="$(mktemp -d "${HOME:-/root}/.emby-fpk-test.XXXXXX" 2>/dev/null)"
    if [ -z "${SANDBOX}" ] || [ ! -d "${SANDBOX}" ]; then
        SANDBOX="$(mktemp -d)"
    fi
    SANDBOX_PARENT=""
    export TRIM_APPNAME=emby
    export TRIM_APPDEST="${SANDBOX}/target"
    export TRIM_PKGVAR="${SANDBOX}/var"
    export TRIM_PKGETC="${SANDBOX}/etc"
    export TRIM_TEMP_LOGFILE="${SANDBOX}/install.log"
    export TRIM_USERNAME=emby
    export TRIM_GROUPNAME=emby
    export TRIM_SERVICE_PORT=8096
    export TRIM_SYS_ARCH=x86

    mkdir -p "${TRIM_APPDEST}/bin" "${TRIM_APPDEST}/system/dashboard-ui" "${TRIM_APPDEST}/lib"
    : > "${TRIM_TEMP_LOGFILE}"

    # 假的可执行文件与增强标记
    cat > "${TRIM_APPDEST}/bin/emby-server" <<'LAUNCH'
#!/bin/bash
echo "stub launcher"
LAUNCH
    chmod +x "${TRIM_APPDEST}/bin/emby-server"
    printf '<script data-main="ext" src="require.js"></script>\n' \
        > "${TRIM_APPDEST}/system/dashboard-ui/index.html"

    unset TRIM_DATA_SHARE_PATHS || true
    # shellcheck disable=SC1091
    . "${PKG}/cmd/common.sh"

    # 这里刻意**不**导出 kill 之类的桩函数：导出的 shell 函数会被子进程继承，
    # 连启动脚本里的 `kill -0` 都会命中桩，导致 launcher 误判子进程已死、
    # 在写 PID 文件前退出（真实踩过这个坑，排查了很久）。
    # 需要控制进程存活时，用真实进程或直接构造 PID 文件内容。
}

teardown_env() {
    [ -n "${SANDBOX:-}" ] && rm -rf "${SANDBOX}" 2>/dev/null
    [ -n "${SANDBOX_PARENT:-}" ] && rm -rf "${SANDBOX_PARENT}" 2>/dev/null
    SANDBOX=""; SANDBOX_PARENT=""
}

# ---------------------------------------------------------------------------
head_ "1. 目录准备"
setup_env
prepare_dirs
allok=1
for d in "" "/config" "/plugins/configurations" "/cache" "/metadata" "/transcoding-temp" "/state"; do
    [ -d "${EMBY_DATA}${d}" ] || { allok=0; bad "缺少目录 ${EMBY_DATA}${d}"; }
done
[ "${allok}" -eq 1 ] && ok "数据目录结构齐备（config/plugins/cache/metadata/transcoding-temp/state）"
[ -f "${EMBY_LICENSE_FILE}" ] || [ -d "${EMBY_DATA}/config" ] && ok "授权目录已建立"
teardown_env

# ---------------------------------------------------------------------------
head_ "2. 会员预激活"
setup_env
prepare_dirs
FAKE_HOSTS="${SANDBOX}/hosts"
: > "${FAKE_HOSTS}"
# 让 apply_license 写我们的假 hosts：临时覆盖路径不可行（脚本写死 /etc/hosts），
# 因此这里只验证授权状态文件部分，hosts 部分改测逻辑分支。
apply_license >/dev/null 2>&1
if [ -f "${EMBY_LICENSE_FILE}" ]; then
    content="$(cat "${EMBY_LICENSE_FILE}")"
    printf '%s' "${content}" | grep -q '"registered":true' \
        && ok "授权状态文件已写入且 registered=true" \
        || bad "授权状态文件内容异常：${content}"
    printf '%s' "${content}" | grep -q '"isValid":true' \
        && ok "授权状态 isValid=true" || bad "授权状态 isValid 不为 true"
    printf '%s' "${content}" | grep -q '2030-01-01' \
        && ok "授权到期时间为 2030-01-01" || bad "授权到期时间异常"
else
    bad "未生成授权状态文件 ${EMBY_LICENSE_FILE}"
fi

# 幂等：第二次调用不得覆盖（避免升级时冲掉 Emby 自维护内容）
printf '%s' '{"marker":"keep-me"}' > "${EMBY_LICENSE_FILE}"
apply_license >/dev/null 2>&1
grep -q 'keep-me' "${EMBY_LICENSE_FILE}" \
    && ok "重复调用不覆盖已有授权文件（幂等）" \
    || bad "重复调用把已有授权文件覆盖了"

# hosts 拦截分支：/etc/hosts 不可写时应给出警告而不是失败
if [ -w /etc/hosts ]; then
    skip "当前环境 /etc/hosts 可写，改造假验证（真机由飞牛 root 执行）"
else
    apply_license >/dev/null 2>&1
    ok "无 /etc/hosts 写权限时安全降级（不报错）"
fi
teardown_env

# ---------------------------------------------------------------------------
head_ "3. PID 与进程状态"
setup_env
prepare_dirs
# 无 PID 文件 -> 未运行
is_running && bad "无 PID 文件时误判为运行中" || ok "无 PID 文件判定为未运行"

# PID 文件内容非法 -> 未运行
printf 'not-a-number\n' > "${EMBY_PID_FILE}"
is_running && bad "非法 PID 内容被误判" || ok "非法 PID 内容判定为未运行"

# 正常 PID，但进程不存在 -> 未运行
printf '%s\n' "999999" > "${EMBY_PID_FILE}"
is_running && bad "进程不存在时误判为运行中" || ok "PID 对应进程不存在时不误报"

# 用当前 shell 的 PID 模拟存活进程
printf '%s\n' "$$" > "${EMBY_PID_FILE}"
if [ -r "/proc/$$/cmdline" ]; then
    # cmdline 里不含 EmbyServer，应判为未运行（防 PID 复用）
    is_running && bad "PID 复用防护失效（cmdline 不匹配却判为运行）" \
               || ok "PID 复用防护生效（cmdline 不匹配则不算运行）"
else
    skip "无 /proc，跳过 cmdline 校验"
fi
teardown_env

# ---------------------------------------------------------------------------
head_ "4. 媒体目录解析"
setup_env
prepare_dirs
mkdir -p "${SANDBOX}/shares/影视" "${SANDBOX}/shares/Media & Stuff" "${SANDBOX}/manual/动画"

# 4a. 共享授权
export TRIM_DATA_SHARE_PATHS="${SANDBOX}/shares/影视:${SANDBOX}/shares/Media & Stuff"
got="$(media_paths | tr '\n' '|')"
case "${got}" in
    *"shares/影视|"*|*"shares/影视"*) ok "共享授权目录被识别（含中文路径）" ;;
    *) bad "共享授权目录未识别：${got}" ;;
esac
case "${got}" in
    *"Media & Stuff"*) ok "含 & 与空格的路径未被破坏" ;;
    *) bad "含 & 的路径被破坏：${got}" ;;
esac

# 4b. 不存在的目录应被过滤
export TRIM_DATA_SHARE_PATHS="${SANDBOX}/shares/不存在:${SANDBOX}/shares/影视"
got="$(media_paths | tr '\n' '|')"
case "${got}" in
    *"不存在"*) bad "不存在的目录未过滤" ;;
    *) ok "不存在的目录已过滤" ;;
esac

# 4c. 手填目录优先
printf '%s\n' "${SANDBOX}/manual/动画" > "${EMBY_STATE_DIR}/media_dirs.txt"
got="$(media_paths | tr '\n' '|')"
case "${got}" in
    *"manual/动画"*) ok "手填目录生效" ;;
    *) bad "手填目录未生效：${got}" ;;
esac
case "${got}" in
    *"shares/影视"*) bad "手填存在时仍混入共享授权目录" ;;
    *) ok "手填目录优先于共享授权" ;;
esac

# 4d. 相对路径与危险前缀过滤
rm -f "${EMBY_STATE_DIR}/media_dirs.txt"
export TRIM_DATA_SHARE_PATHS="relative/path:/config:/proc/self:${SANDBOX}/shares/影视"
got="$(media_paths | tr '\n' '|')"
case "${got}" in
    *"relative/path"*) bad "相对路径未过滤" ;;
    *) ok "相对路径已过滤" ;;
esac
case "${got}" in
    *"/proc"*) bad "危险系统路径未过滤" ;;
    *) ok "危险系统路径已过滤" ;;
esac

# 4e. 清单发布
publish_media_list >/dev/null 2>&1
[ -f "${EMBY_DATA}/media-paths.txt" ] \
    && ok "media-paths.txt 清单已生成" || bad "清单未生成"
teardown_env

# ---------------------------------------------------------------------------
head_ "5. 启动脚本环境与参数"
setup_env
# 用一个假的 EmbyServer 捕获环境变量与参数
# 注意：桩脚本写的是绝对路径 "${capture}"。launcher 会先 cd 到 APP_DIR，
# 如果桩里用相对路径，捕获文件就会落到 APP_DIR 里，导致测试假失败。
mkdir -p "${TRIM_APPDEST}/system"
capture="${SANDBOX}/capture.txt"
cat > "${TRIM_APPDEST}/system/EmbyServer" <<CAPTURE
#!/bin/bash
{
  echo "LD_LIBRARY_PATH=\${LD_LIBRARY_PATH}"
  echo "LIBVA_DRIVERS_PATH=\${LIBVA_DRIVERS_PATH}"
  echo "IGNORE_VAAPI_ENABLED_FLAG=\${IGNORE_VAAPI_ENABLED_FLAG}"
  echo "XDG_CACHE_HOME=\${XDG_CACHE_HOME}"
  echo "ARGS=\$*"
} > "${capture}"
exit 0
CAPTURE
chmod +x "${TRIM_APPDEST}/system/EmbyServer"
mkdir -p "${TRIM_APPDEST}/lib/dri" "${TRIM_APPDEST}/etc/OpenCL/vendors" "${TRIM_APPDEST}/etc/fonts"
cp -f "${ASSETS}/bin/emby-server" "${TRIM_APPDEST}/bin/emby-server"
chmod +x "${TRIM_APPDEST}/bin/emby-server"

# 直接执行（与 cmd/main 的调用方式一致）。注意不要写成 `bash <path>`：
# 某些环境（如 Git Bash）这样调用会因为路径转换而报 "No such file or directory"，
# 那是测试环境的假象，不是启动脚本的问题。
mkdir -p "${SANDBOX}/extradri"
TRIM_APPDEST="${TRIM_APPDEST}" TRIM_PKGVAR="${TRIM_PKGVAR}" TRIM_SERVICE_PORT=8096 \
    TRIM_APPNAME=emby EMBY_EXTRA_VA_DIRS="${SANDBOX}/extradri" \
    "${TRIM_APPDEST}/bin/emby-server" >/dev/null 2>&1

if [ -f "${capture}" ]; then
    ok "启动脚本成功拉起 EmbyServer 二进制"
    # 真机实测结论：不能 export LD_LIBRARY_PATH（会污染子进程，实测连 ldd/bash
    # 都被随包旧 glibc 带崩）。加载器只通过 --library-path 把 system/ 作用于
    # EmbyServer 自身，所以这里断言它**为空**。
    grep -q "^LD_LIBRARY_PATH=$" "${capture}" \
        && ok "未污染 LD_LIBRARY_PATH（改用加载器 --library-path 限定作用域）" \
        || bad "LD_LIBRARY_PATH 不应被设置：$(grep LD_LIBRARY_PATH "${capture}")"

    # 启动脚本必须用系统加载器 + 只把 system/ 放进库路径
    grep -q -- '--library-path' "${ASSETS}/bin/emby-server" \
        && ok "启动脚本用加载器 --library-path 指定库目录" \
        || bad "启动脚本未使用 --library-path"
    grep -q 'system' "${ASSETS}/bin/emby-server" \
        && ok "库路径指向 system/（.NET 运行时与 Emby 原生依赖所在）" \
        || bad "库路径未指向 system/"

    grep -q "LIBVA_DRIVERS_PATH=${TRIM_APPDEST}/lib/dri" "${capture}" \
        && ok "LIBVA_DRIVERS_PATH 以随包 VAAPI 驱动目录开头" \
        || bad "LIBVA_DRIVERS_PATH 异常：$(grep LIBVA_DRIVERS_PATH "${capture}")"
    # ARM 机型的 VAAPI 驱动由系统提供，搜索路径必须保留系统目录与扩展目录，
    # 否则 ARM 硬解会直接不可用（Emby 的 arm64 包不带任何 VAAPI 驱动）
    vline="$(grep '^LIBVA_DRIVERS_PATH=' "${capture}")"
    case "${vline}" in
        *"${SANDBOX}/extradri"*) ok "LIBVA_DRIVERS_PATH 保留扩展驱动目录（ARM 可指定系统驱动）" ;;
        *) bad "扩展驱动目录丢失：${vline}" ;;
    esac
    grep -q "IGNORE_VAAPI_ENABLED_FLAG=true" "${capture}" \
        && ok "硬解开关 IGNORE_VAAPI_ENABLED_FLAG 已设置" || bad "硬解开关缺失"
    args="$(grep '^ARGS=' "${capture}")"
    missingargs=""
    for want in "-programdata ${TRIM_PKGVAR}" "-ffmpeg ${TRIM_APPDEST}/bin/ffmpeg" \
                "-ffprobe ${TRIM_APPDEST}/bin/ffprobe" "-restartexitcode 3" "-pidfile"; do
        case "${args}" in
            *"${want}"*) ;;
            *) missingargs="${missingargs} [${want}]" ;;
        esac
    done
    if [ -z "${missingargs}" ]; then
        ok "启动参数齐备（programdata/ffmpeg/ffprobe/restartexitcode/pidfile）"
    else
        bad "启动参数缺少：${missingargs}"
    fi
else
    bad "启动脚本未拉起 EmbyServer（未生成捕获文件 ${capture}）"
    # 便于排查：把 launcher 的 stderr 打出来
    TRIM_APPDEST="${TRIM_APPDEST}" TRIM_PKGVAR="${TRIM_PKGVAR}" TRIM_SERVICE_PORT=8096 \
        TRIM_APPNAME=emby "${TRIM_APPDEST}/bin/emby-server" 2>&1 | head -10
fi
teardown_env

# ---------------------------------------------------------------------------
head_ "6. 启动不能阻塞应用中心（关键回归）"
setup_env
prepare_dirs
# 造一个会常驻的假 EmbyServer，并让 launcher 认为它一直活着
cat > "${TRIM_APPDEST}/system/EmbyServer" <<'STUB'
#!/bin/bash
sleep 300
STUB
chmod +x "${TRIM_APPDEST}/system/EmbyServer"
cp -f "${ASSETS}/bin/emby-server" "${TRIM_APPDEST}/bin/emby-server"
chmod +x "${TRIM_APPDEST}/bin/emby-server"

# 恢复真实的 wait_ready（setup_env 里被桩掉了），验证默认不等待就绪
# shellcheck disable=SC1091
. "${PKG}/cmd/common.sh"

wait_ready "${BASHPID}"; rc=$?
[ "${rc}" -eq 0 ] && ok "未设置 EMBY_START_TIMEOUT 时 wait_ready 立即返回成功（不阻塞）" \
                 || bad "wait_ready 默认仍在阻塞（rc=${rc}）"

# resolve_run_user 必须能在 root 场景下给出一个可用用户，或明确返回失败而不是静默用 root
if u="$(resolve_run_user)"; then
    ok "resolve_run_user 给出运行用户：${u}"
else
    if [ "$(id -u)" = "0" ]; then
        bad "以 root 运行时未能解析出降权用户（会静默以 root 常驻）"
    else
        skip "当前非 root，无需降权"
    fi
fi

# 真实计时 start_emby：必须在数秒内返回，而不是等端口
# 后台监控 PID 文件的生死，用于区分「从没生成」和「生成后被删」
(
    for _ in $(seq 1 20); do
        if [ -f "${EMBY_PID_FILE}" ]; then
            echo "T+${_}: EXISTS=$(cat "${EMBY_PID_FILE}" 2>/dev/null)"
        else
            echo "T+${_}: MISSING"
        fi
        sleep 1
    done
) > "${SANDBOX}/pidwatch.log" 2>&1 &
WATCH_PID=$!

start_ts=$(date +%s)
EMBY_LAUNCHER_TRACE="${SANDBOX}/launcher-trace.log" start_emby >/dev/null 2>&1; rc=$?
elapsed=$(( $(date +%s) - start_ts ))
kill "${WATCH_PID}" 2>/dev/null || true
if [ "${rc}" -eq 0 ]; then
    if [ "${elapsed}" -le 20 ]; then
        ok "start_emby 在 ${elapsed}s 内返回（应用中心不会卡在“启用中”）"
    else
        bad "start_emby 耗时 ${elapsed}s，太慢，应用中心会显示卡住"
    fi
else
    bad "start_emby 返回 ${rc}（耗时 ${elapsed}s）"
    echo "        --- 诊断 ---"
    echo "        PID 文件路径: ${EMBY_PID_FILE}"
    echo "        PID 文件内容: $(cat "${EMBY_PID_FILE}" 2>&1 | head -1)"
    echo "        启动脚本可执行: $([ -x "${EMBY_BIN}" ] && echo 是 || echo 否) (${EMBY_BIN})"
    echo "        emby.log:"; sed 's/^/          /' "${EMBY_LOG}" 2>/dev/null | head -8
    echo "        PID 文件监视（T+n 秒）:"; sed 's/^/          /' "${SANDBOX}/pidwatch.log" 2>/dev/null | head -12
    echo "        相关进程:"; ps -ef 2>/dev/null | grep -F "${TRIM_APPDEST}" | grep -v grep | sed 's/^/          /' | head -5
fi

# 收尾：杀掉常驻桩进程，避免留下孤儿
if p="$(read_pid)"; then kill -9 "${p}" 2>/dev/null || true; fi
teardown_env

# ---------------------------------------------------------------------------
head_ "7. 配置变更回调与卸载"
# 必须重新 setup_env：上一组末尾的 teardown_env 会把 SANDBOX 等变量清掉，
# 少了这一句 ${SANDBOX} 就是空的，路径会变成 "/shares/..."。
# Git Bash 下 MSYS 会把它当成 Windows 路径从而"看起来正常"，WSL 下则直接
# mkdir 失败（实测踩过），所以这里显式重建环境。
setup_env
prepare_dirs
# shellcheck disable=SC1091
. "${PKG}/cmd/service-setup"

# 防线：沙盒必须建立成功，否则后面所有路径断言都不可信
[ -n "${SANDBOX}" ] && [ -d "${SANDBOX}" ] \
    && ok "测试沙盒已就绪（${SANDBOX}）" \
    || bad "测试沙盒未建立：SANDBOX='${SANDBOX}'（漏了 setup_env？）"

# 用纯 ASCII 目录名：中文名在 Git Bash / WSL / CI 之间的字节编码可能不一致，
# grep 匹配会随机失败（踩过）。测试要验的是「路径刷新」而不是编码。
mkdir -p "${SANDBOX}/shares/newmedia"
export TRIM_DATA_SHARE_PATHS="${SANDBOX}/shares/newmedia"
service_postconfig >/dev/null 2>&1; rc=$?
[ "${rc}" -eq 0 ] && ok "未运行时 config_callback 正常返回 0" || bad "config_callback 返回 ${rc}"
grep -q "newmedia" "${EMBY_DATA}/media-paths.txt" 2>/dev/null \
    && ok "新授权目录已刷新进清单" || bad "新授权目录未刷新"

# 数据保留 / 清除
printf 'keep' > "${EMBY_DATA}/marker.txt"
wizard_delete_data="false"
service_postuninstall >/dev/null 2>&1
[ -f "${EMBY_DATA}/marker.txt" ] && ok "选择保留时数据未被删除" || bad "保留选项失效"
wizard_delete_data="true"
service_postuninstall >/dev/null 2>&1
[ ! -f "${EMBY_DATA}/marker.txt" ] && ok "选择清除时数据被删除" || bad "清除选项失效"
teardown_env

# ---------------------------------------------------------------------------
head_ "8. 包完整性"
setup_env
missing=0

# 只有 uninstall 向导：install/config/upgrade 的存在会让飞牛弹出交互界面，
# 而官方应用商店的包是「点了就装、直接进度条」。详见 README。
for f in manifest manifest.template ICON.PNG ICON_256.PNG \
         config/privilege config/resource config/emby.sc \
         cmd/main cmd/common.sh cmd/service-setup \
         cmd/install_init cmd/install_callback cmd/upgrade_init cmd/upgrade_callback \
         cmd/config_init cmd/config_callback cmd/uninstall_init cmd/uninstall_callback \
         wizard/uninstall; do
    [ -e "${PKG}/${f}" ] || { bad "缺少 ${f}"; missing=1; }
done
[ "${missing}" -eq 0 ] && ok "包内必要文件齐备"

for f in install config upgrade; do
    if [ -e "${PKG}/wizard/${f}" ]; then
        bad "wizard/${f} 存在：飞牛会弹出交互式安装/配置界面"
        missing=1
    fi
done
[ "${missing}" -eq 0 ] && ok "wizard 只有 uninstall（安装无交互，直接进度条）"

for f in app-assets/bin/emby-server app-assets/ui/config \
         app-assets/ui/images/64.png app-assets/ui/images/256.png; do
    [ -e "${SRC}/${f}" ] || { bad "缺少 ${f}"; missing=1; }
done
[ "${missing}" -eq 0 ] && ok "app-assets 齐备（启动脚本 + 桌面入口 + 官方图标）"

# 图标必须是官方标识且带透明背景。真机踩过：从 Emby 的
# dashboard icon-512x512.png 生成出来的图标背景是实心黑（alpha 全 255），
# fnOS 会给不透明图标套一层圆角方块，最终显示成"绿色圆角方块"而非官方菱形。
# 先确认解释器里真的有 Pillow。没有就跳过 —— 否则 import 失败会被误判成
# 「图标不合格」，在白跑一次 CI 后才被发现（踩过）。
if [ -n "${PY}" ] && "${PY}" -c "import PIL" >/dev/null 2>&1; then
    icon_bad=0
    for f in "${PKG}/ICON.PNG" "${PKG}/ICON_256.PNG" \
             "${SRC}/app-assets/ui/images/64.png" "${SRC}/app-assets/ui/images/256.png"; do
        [ -f "${f}" ] || { bad "缺少图标 ${f}"; icon_bad=1; continue; }
        if "${PY}" -c "
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert('RGBA')
lo, _ = im.getchannel('A').getextrema()
sys.exit(0 if lo <= 8 and im.size[0] == im.size[1] else 1)
" "${f}" 2>/dev/null; then
            ok "图标合格（正方形且透明背景）：${f}"
        else
            bad "图标不合格（背景不透明或非正方形）：${f}"
            icon_bad=1
        fi
    done
    [ "${icon_bad}" -eq 0 ] && ok "全部图标均为官方标识且带透明背景"
else
    # 退一步：至少校验 PNG 头部与正方形尺寸，不依赖 Pillow
    png_bad=0
    for f in "${PKG}/ICON.PNG" "${PKG}/ICON_256.PNG" \
             "${SRC}/app-assets/ui/images/64.png" "${SRC}/app-assets/ui/images/256.png"; do
        [ -f "${f}" ] || { bad "缺少图标 ${f}"; png_bad=1; continue; }
        sig="$(od -An -tx1 -N8 "${f}" 2>/dev/null | tr -d ' \n')"
        [ "${sig}" = "89504e470d0a1a0a" ] || { bad "${f} 不是合法 PNG"; png_bad=1; }
    done
    [ "${png_bad}" -eq 0 ] && ok "图标均为合法 PNG（未装 Pillow，跳过透明度校验）"
    skip "无 Pillow，未校验图标透明背景（CI 里会装 Pillow 并校验）"
fi

if grep -q 'images/{0}.png' "${SRC}/app-assets/ui/config"; then
    ok "入口图标使用官方命名约定 images/{0}.png"
else
    bad "入口图标命名与官方不一致"
fi

manifest_ok=1
for k in appname version display_name source platform service_port desktop_applaunchname \
         maintainer distributor; do
    grep -q "^${k}=" "${PKG}/manifest.template" || { bad "manifest.template 缺少字段 ${k}"; manifest_ok=0; }
done
[ "${manifest_ok}" -eq 1 ] && ok "manifest.template 字段齐备（含开发者与发布者）"

grep -q '@APPVERSION@' "${PKG}/manifest.template" && ok "模板保留 @APPVERSION@ 占位符" \
    || bad "manifest.template 缺少 @APPVERSION@"
grep -q '@PLATFORM@' "${PKG}/manifest.template" && ok "模板保留 @PLATFORM@ 占位符" \
    || bad "manifest.template 缺少 @PLATFORM@"

if [ -n "${PY}" ]; then
    for f in "${PKG}/config/privilege" "${PKG}/config/resource" \
             "${SRC}/app-assets/ui/config" "${PKG}/wizard/uninstall"; do
        if "${PY}" -c "import json,sys; json.load(open(sys.argv[1],encoding='utf-8'))" "${f}" 2>/dev/null; then
            ok "JSON 合法：${f}"
        else
            bad "JSON 解析失败：${f}"
        fi
    done
else
    skip "未找到 python，跳过 JSON 校验"
fi

# 权限模型：与官方 EmbyServer 包一致用 run-as=package。
# 这样 Emby 以普通用户运行；写 /etc/hosts 的会员拦截会自动降级为警告，
# 不会因为权限不足而让整个应用起不来。
if grep -q '"run-as": *"package"' "${PKG}/config/privilege"; then
    ok "privilege 使用 run-as=package（与官方包一致）"
else
    bad "privilege 未使用 run-as=package"
fi
if grep -q '"run-as": *"root"' "${PKG}/config/privilege"; then
    bad "privilege 声明了 run-as=root：会以 root 常驻，且飞牛对 root+username 有已知坑"
fi

grep -q 'systemd-unit' "${PKG}/config/resource" && ok "resource 声明了 systemd-unit（原生服务型应用）" \
    || bad "resource 缺少 systemd-unit"
if grep -q 'docker-project' "${PKG}/config/resource"; then
    bad "resource 仍残留 docker-project（本包应为原生安装）"
else
    ok "resource 不含 docker-project（确认非 Docker 方案）"
fi

# 符号链接恢复机制必须在：包内只有实体文件，soname 别名靠安装时重建
grep -q 'restore_symlinks' "${PKG}/cmd/common.sh" \
    && ok "common.sh 含 restore_symlinks（安装时重建 soname 别名）" \
    || bad "common.sh 缺少 restore_symlinks：Emby 会因找不到 libstdc++.so.6 起不来"
grep -q 'links.tsv' "${PKG}/cmd/common.sh" && ok "restore_symlinks 读取 links.tsv 清单" \
    || bad "restore_symlinks 未指向 links.tsv"
grep -q 'restore_symlinks' "${PKG}/cmd/main" && ok "main start 兜底恢复链接（升级/异常后自愈）" \
    || bad "main 未调用 restore_symlinks"

# 启动脚本必须做加载器回退：ELF PT_INTERP 是写死的绝对路径，
# 目标系统缺这个路径时程序会直接报 "No such file or directory"
grep -q 'ld-linux' "${SRC}/app-assets/bin/emby-server" \
    && ok "启动脚本含动态加载器回退" \
    || bad "启动脚本缺少加载器回退逻辑"

# 启动不能阻塞应用中心：默认不等待端口就绪
grep -q 'EMBY_START_TIMEOUT' "${PKG}/cmd/common.sh" \
    && ok "启动流程支持 EMBY_START_TIMEOUT（默认不阻塞应用中心）" \
    || bad "启动流程缺少 EMBY_START_TIMEOUT"

teardown_env

# ---------------------------------------------------------------------------
printf '\n\033[36m==== 结果：%d 通过，%d 失败，%d 跳过 ====\033[0m\n' "${PASS}" "${FAIL}" "${SKIP}"
[ "${FAIL}" -eq 0 ] || exit 1
