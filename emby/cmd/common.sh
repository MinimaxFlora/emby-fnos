#!/bin/bash
# =============================================================================
#  Emby for fnOS — 原生安装版共享逻辑库
#  被 cmd/main、cmd/service-setup 引用，不要当生命周期脚本单独执行。
#
#  与 Docker 版的根本区别：Emby 是直接跑在 NAS 上的原生进程，不依赖 Docker。
# =============================================================================

EMBY_APPNAME="emby"
EMBY_PROC="EmbyServer"
EMBY_CONTAINER_PORT="8096"

# 运行目录（由飞牛注入，禁止硬编码安装路径）
EMBY_HOME="${TRIM_APPDEST}"                 # 程序本体（只读）
EMBY_DATA="${TRIM_PKGVAR}"                  # 运行数据（programdata，跨升级保留）
EMBY_BIN="${EMBY_HOME}/bin/emby-server"
EMBY_PID_FILE="${EMBY_DATA}/emby.pid"
EMBY_LOG="${EMBY_DATA}/emby.log"
EMBY_STATE_DIR="${EMBY_DATA}/state"

# 授权预置数据（不随仓库分发）
#
# 本包可以在安装时把一份「授权状态」写入应用数据目录，具体内容**不在仓库里**，
# 而是构建时由 CI 从仓库 Secret 注入，落成一个配置文件随包分发：
#
#     <包根>/config/license.json
#     {
#       "filename": "<授权状态文件的文件名>",     # 可选
#       "state":    "<该文件的内容>",
#       "hosts":    "<可选的域名拦截条目>"        # 可选
#     }
#
# 文件不存在（或缺少 state）时整体跳过 —— 这样没有 Secret 的构建（例如别人
# fork 后自己编）依然能正常装、正常跑，只是不带预置授权状态。
EMBY_LICENSE_DIR="${EMBY_DATA}/config"
EMBY_LICENSE_DATA="${EMBY_HOME}/config/license.json"
# 由 apply_license() 从 EMBY_LICENSE_DATA 里解析出来后填充
EMBY_LICENSE_FILE=""

log() {
    local line="[emby] $*"
    # 日志一律走 stderr。若走 stdout，任何 x="$(some_func)" 形式的命令替换都会
    # 把日志文本一起抓进变量 —— 实测导致 run_user 变成
    # 「emby] 提示：用户 ... 无写权限」，传给 runuser 直接报用户不存在。
    echo "${line}" >&2
    [ -n "${TRIM_TEMP_LOGFILE:-}" ] && echo "${line}" >> "${TRIM_TEMP_LOGFILE}" 2>/dev/null
    [ -n "${EMBY_LOG:-}" ] && echo "$(date '+%Y-%m-%d %H:%M:%S') ${line}" >> "${EMBY_LOG}" 2>/dev/null
    return 0
}

# ---------------------------------------------------------------------------
# 恢复符号链接（soname 别名）
#
# 为什么必须在 NAS 上做：
#   Debian 系把共享库的 soname 别名做成符号链接
#   （libstdc++.so.6 -> libstdc++.so.6.0.32），动态链接器按 soname 找文件。
#   而本包的构建链跑在 Windows 上 —— Windows 创建符号链接需要管理员权限或
#   开发者模式（WinError 1314），也看不见 WSL 在 /mnt/d 上建的链接（WinError 1920）。
#   结果就是包内这些别名全部丢失，EmbyServer 会因为找不到 libstdc++.so.6
#   直接启动失败。所以清单随包分发，安装时用原生 ln -s 补回来。
#
# 幂等：已存在且指向正确的链接跳过；指向错误才重建。
# ---------------------------------------------------------------------------
LINK_MANIFEST="${EMBY_HOME}/links.tsv"

restore_symlinks() {
    [ -f "${LINK_MANIFEST}" ] || {
        log "提示：未找到 ${LINK_MANIFEST}，跳过符号链接恢复"
        return 0
    }

    local made=0 skipped=0 failed=0 missing_target=0
    local rel target dest resolved

    while IFS=$'\t' read -r rel target; do
        # 跳过注释与空行
        case "${rel}" in ''|\#*) continue ;; esac
        [ -n "${target}" ] || continue

        dest="${EMBY_HOME}/${rel}"
        # 链接目标：绝对路径按负载根解析，相对路径相对链接自身所在目录
        case "${target}" in
            /*) resolved="${EMBY_HOME}${target}" ;;
            *)  resolved="$(dirname "${dest}")/${target}" ;;
        esac

        # 目标不存在就别建，否则留下坏链接反而更难排查
        if [ ! -e "${resolved}" ]; then
            missing_target=$((missing_target + 1))
            continue
        fi

        if [ -L "${dest}" ] && [ "$(readlink "${dest}" 2>/dev/null)" = "${target}" ]; then
            skipped=$((skipped + 1))
            continue
        fi

        mkdir -p "$(dirname "${dest}")" 2>/dev/null || true
        rm -f "${dest}" 2>/dev/null || true
        if ln -s "${target}" "${dest}" 2>/dev/null; then
            made=$((made + 1))
        else
            failed=$((failed + 1))
            [ "${failed}" -le 3 ] && log "警告：创建链接失败 ${rel} -> ${target}"
        fi
    done < "${LINK_MANIFEST}"

    log "符号链接：新建 ${made}，已正确 ${skipped}，失败 ${failed}，目标缺失 ${missing_target}"
    [ "${failed}" -eq 0 ]
}

# 关键运行时库自检：缺了必然起不来，提前给出可读的错误。
# 注意加载器有两个可能的名字（x86-64 与 aarch64 各一个），只要求存在其一。
verify_runtime_libs() {
    local missing="" n
    local has_loader=0
    for n in ld-linux-x86-64.so.2 ld-linux-aarch64.so.1; do
        [ -e "${EMBY_HOME}/lib/${n}" ] && has_loader=1
    done
    [ "${has_loader}" -eq 0 ] && missing="${missing} ld-linux-*.so.*"

    for n in libc.so.6 libstdc++.so.6 libm.so.6 libgcc_s.so.1; do
        [ -e "${EMBY_HOME}/lib/${n}" ] || missing="${missing} ${n}"
    done

    if [ -n "${missing}" ]; then
        log "警告：随包运行时库缺失：${missing}"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 为 ffmpeg / ffprobe / ffdetect 生成加载器包装
#
# 为什么需要：
#   这三个二进制与 EmbyServer 一样，PT_INTERP 写死 /lib/ld-linux-x86-64.so.2，
#   而 Debian 12 用 multiarch 布局、该路径不存在 —— 直接执行会 ENOENT，
#   在 Emby 日志里表现为 `Win32Exception (2): An error occurred trying to start
#   process '/vol1/@appcenter/emby/bin/ffmpeg'`，转码直接不可用。
#   而且它们是共享链接的，需要随包 lib/ 里的 libav*。
#
# 做法：原文件改名为 <tool>.real，放一个包装脚本占原位置，
# 包装脚本用系统加载器 --library-path <app>/lib:<app>/bin 拉起 .real。
#
# **时机很重要**：函数幂等、可重复调用，但必须在 bin/ 已经铺好之后调用。
# 实测 service_preinst 阶段 bin/ 往往还没就位，此时会一件都处理不了且静默通过；
# 所以 service_postinst / service_postupgrade / main start 三处都要兜底调用。
# ---------------------------------------------------------------------------
WRAPPED_TOOLS="ffmpeg ffprobe ffdetect"

find_system_loader() {
    local n
    for n in /lib/x86_64-linux-gnu/ld-linux-x86-64.so.2 \
             /lib64/ld-linux-x86-64.so.2 \
             /lib/ld-linux-x86-64.so.2 \
             /lib/aarch64-linux-gnu/ld-linux-aarch64.so.1 \
             /lib64/ld-linux-aarch64.so.1 \
             /lib/ld-linux-aarch64.so.1; do
        [ -e "${n}" ] && { printf '%s' "${n}"; return 0; }
    done
    return 1
}

install_tool_wrappers() {
    local loader tool real wrap
    loader="$(find_system_loader || true)"
    [ -n "${loader}" ] || log "警告：找不到系统动态加载器，ffmpeg/ffprobe/ffdetect 可能无法执行"

    local made=0 already=0 waiting=0 failed=0
    for tool in ${WRAPPED_TOOLS}; do
        real="${EMBY_HOME}/bin/${tool}.real"
        wrap="${EMBY_HOME}/bin/${tool}"

        if [ -x "${real}" ]; then
            already=$((already + 1))      # 已包装过（升级或重复调用）
            continue
        fi
        if [ ! -f "${wrap}" ]; then
            # 二进制还没铺好 —— 不是错误，调用方会在更晚的阶段重试
            waiting=$((waiting + 1))
            continue
        fi
        if ! mv -f "${wrap}" "${real}" 2>/dev/null; then
            log "警告：无法把 bin/${tool} 改名为 .real（目录不可写？）"
            failed=$((failed + 1))
            continue
        fi

        cat > "${wrap}" <<WRAPPER
#!/bin/bash
# Emby for fnOS：${tool} 的启动包装（由 cmd/common.sh 生成）。
# 原因：该二进制的 PT_INTERP 写死 /lib/ld-linux-x86-64.so.2，Debian 12 上不存在；
# 且它是共享链接的，需要随包 lib/ 里的 libav* 库。这里用系统加载器显式拉起，
# 并用 --library-path 把作用域限制在本次调用，避免污染全局库搜索路径。
APP_DIR="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")/.." && pwd)"
LOADER="${loader}"
if [ -x "\${LOADER}" ]; then
    exec "\${LOADER}" --library-path "\${APP_DIR}/lib:\${APP_DIR}/bin" \\
         "\${APP_DIR}/bin/${tool}.real" "\$@"
fi
exec "\${APP_DIR}/bin/${tool}.real" "\$@"
WRAPPER
        chmod +x "${wrap}" 2>/dev/null || true
        made=$((made + 1))
    done

    local msg="转码工具包装：新建 ${made}，已就位 ${already}"
    [ "${waiting}" -gt 0 ] && msg="${msg}，待铺好 ${waiting}"
    [ "${failed}" -gt 0 ] && msg="${msg}，失败 ${failed}"
    log "${msg}"

    # 全部待铺好说明调用时机过早，明确说出来便于排查
    [ "${waiting}" -gt 0 ] && [ "${made}" -eq 0 ] && [ "${already}" -eq 0 ] \
        && log "提示：bin/ 下尚无待包装的二进制，稍后会在安装/启动阶段重试"

    # 有工具存在但没包装成功才算失败
    [ "${failed}" -eq 0 ]
}

# ---------------------------------------------------------------------------
# 把 Emby 的监听端口对齐到飞牛分配的端口
#
# 为什么需要：Emby **没有** -httpport 之类的命令行参数（已从 EmbyServer.dll
# 里核实，只有 -programdata / -nolocalportconfig 等），端口只认
# config/system.xml 的 HttpServerPortNumber。而飞牛是按 TRIM_SERVICE_PORT
# 建端口转发和桌面入口的。用户在应用中心改过端口之后两边就不一致 ——
# 进程活着、端口也占着，但飞牛转发的那个端口没人听，表现为「启动成功却打不开」。
#
# 用 python3 改（NAS 自带）：system.xml 是 Emby 自己的序列化格式，
# 定点正则替换比引入 XML 库更稳，也不会动到其它字段。
# ---------------------------------------------------------------------------
sync_http_port() {
    local want="${TRIM_SERVICE_PORT:-}"
    case "${want}" in
        ''|*[!0-9]*) return 0 ;;        # 未注入或非法，交给 Emby 用默认值
    esac
    [ "${want}" -ge 1 ] && [ "${want}" -le 65535 ] || return 0

    local cfg="${EMBY_DATA}/config/system.xml"
    # 首次启动时配置还不存在 —— Emby 会用 8096，与包内默认一致，无需处理
    [ -f "${cfg}" ] || return 0

    local cur
    cur="$(grep -o '<HttpServerPortNumber>[0-9]*</HttpServerPortNumber>' "${cfg}" 2>/dev/null \
           | head -1 | grep -o '[0-9]*')"
    [ "${cur}" = "${want}" ] && return 0

    local py=""
    local c
    for c in python3 python; do
        command -v "${c}" >/dev/null 2>&1 && { py="${c}"; break; }
    done
    if [ -z "${py}" ]; then
        log "警告：系统没有 python，无法把监听端口对齐到 ${want}（当前 ${cur:-未知}）"
        return 0
    fi

    if "${py}" - "${cfg}" "${want}" <<'PYEOF'
import re, sys
path, want = sys.argv[1], sys.argv[2]
with open(path, encoding='utf-8') as f:
    s = f.read()
s2 = re.sub(r'<HttpServerPortNumber>\d+</HttpServerPortNumber>',
            '<HttpServerPortNumber>' + want + '</HttpServerPortNumber>', s)
s2 = re.sub(r'<PublicPort>\d+</PublicPort>',
            '<PublicPort>' + want + '</PublicPort>', s2)
if s2 != s:
    with open(path, 'w', encoding='utf-8') as f:
        f.write(s2)
PYEOF
    then
        log "监听端口已对齐：${cur:-?} -> ${want}（Emby 只认 system.xml，无命令行端口参数）"
    else
        log "警告：改写 ${cfg} 失败，Emby 可能仍在 ${cur:-默认端口} 上监听"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 目录准备
# ---------------------------------------------------------------------------
prepare_dirs() {
    mkdir -p "${EMBY_DATA}" "${EMBY_STATE_DIR}" \
             "${EMBY_DATA}/config" \
             "${EMBY_DATA}/plugins/configurations" \
             "${EMBY_DATA}/cache" \
             "${EMBY_DATA}/metadata" \
             "${EMBY_DATA}/transcoding-temp" 2>/dev/null || true
    return 0
}

# 把数据目录归还给应用用户。因为以 root 启动（需要写 /etc/hosts 与绑定设备），
# 若不交还，Emby 后续以 emby 用户运行时无法写入自己的配置。
restore_owner() {
    local u="${TRIM_USERNAME:-${EMBY_APPNAME}}"
    id "${u}" >/dev/null 2>&1 || return 0
    chown -R "${u}:${TRIM_GROUPNAME:-${u}}" "${EMBY_DATA}" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------------
# 授权预置（数据来自随包的 config/license.json，见文件头说明）
#
#   filename : 授权状态文件的文件名（可选，来自配置而不是写死在脚本里）
#   state    : 写入该文件的内容
#   hosts    : 可选的域名拦截条目（一行，形如 "IP 域名"）
#
# 全部幂等；配置文件不存在或缺 state 则整体跳过，不影响安装与启动。
# 需要 python3 解析 JSON，系统没有可用 python 时只记一条警告。
# ---------------------------------------------------------------------------
apply_license() {
    prepare_dirs

    if [ ! -f "${EMBY_LICENSE_DATA}" ]; then
        log "未随包提供授权预置数据，跳过（应用仍可正常使用与登录）"
        return 0
    fi

    local py=""
    local c
    for c in python3 python; do
        # 不能只看 command -v：某些系统上 python 是个跑不了代码的占位
        # （Windows 的 Microsoft Store 别名就是典型），必须实际执行一次才算数。
        if command -v "${c}" >/dev/null 2>&1 && "${c}" -c 'pass' >/dev/null 2>&1; then
            py="${c}"
            break
        fi
    done
    if [ -z "${py}" ]; then
        log "警告：系统没有可用的 python，无法解析 ${EMBY_LICENSE_DATA}"
        return 0
    fi

    # 一次解析出三个字段，避免重复启动解释器
    local parsed fname state hosts
    parsed="$("${py}" -c "
import json,sys
try:
    d = json.load(open(sys.argv[1], encoding='utf-8'))
except Exception:
    sys.exit(0)
for k in ('filename','state','hosts'):
    v = d.get(k,'') or ''
    print(str(v).replace(chr(10),' '))
" "${EMBY_LICENSE_DATA}" 2>/dev/null)"
    fname="$(printf '%s\n' "${parsed}" | sed -n '1p')"
    state="$(printf '%s\n' "${parsed}" | sed -n '2p')"
    hosts="$(printf '%s\n' "${parsed}" | sed -n '3p')"

    # 1. 授权状态文件：仅在不存在时写入，避免升级时覆盖 Emby 自己维护的内容
    if [ -n "${state}" ]; then
        if [ -z "${fname}" ]; then
            log "警告：授权预置数据缺少 filename 字段，无法定位状态文件"
        else
            EMBY_LICENSE_FILE="${EMBY_LICENSE_DIR}/${fname}"
            if [ ! -f "${EMBY_LICENSE_FILE}" ]; then
                printf '%s\n' "${state}" > "${EMBY_LICENSE_FILE}" 2>/dev/null \
                    && log "已写入授权状态文件" \
                    || log "警告：写入授权状态文件失败（${EMBY_LICENSE_FILE}）"
            else
                log "授权状态文件已存在，保持不变"
            fi
        fi
    fi

    # 2. 域名拦截：幂等；只在 /etc/hosts 可写时执行
    if [ -n "${hosts}" ]; then
        local domain
        domain="$(printf '%s' "${hosts}" | awk '{print $NF}')"
        if [ -w /etc/hosts ] || [ "$(id -u)" = "0" ]; then
            if grep -qF "${domain}" /etc/hosts 2>/dev/null; then
                log "授权校验域名拦截已存在"
            elif printf '%s\n' "${hosts}" >> /etc/hosts 2>/dev/null; then
                log "已写入授权校验域名拦截"
            else
                log "警告：写入 /etc/hosts 失败"
            fi
        else
            log "提示：当前身份无法写 /etc/hosts，跳过域名拦截"
        fi
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 增强插件
#   镜像的 system/dashboard-ui/ 里已自带 require.js + ext.js + embyHappy.js
#   + ede.user.js(弹幕) / actorPlus.js / embyLaunchPotplayer.js 等，
#   且 index.html 已被打过补丁（多出 <script data-main="ext" src="require.js">）。
#   因为 Emby 直接从这个目录提供 Web UI，所以浏览器一打开就自动生效，
#   本包无需再做注入。这里只做一次存在性自检，便于排查。
# ---------------------------------------------------------------------------
check_extensions() {
    local ui="${EMBY_HOME}/system/dashboard-ui"
    local idx="${ui}/index.html"
    if [ ! -f "${idx}" ]; then
        log "警告：未找到 dashboard-ui/index.html，Web 界面可能异常"
        return 0
    fi
    if grep -q 'data-main="ext"' "${idx}" 2>/dev/null; then
        log "增强模块已启用（ext.js + embyHappy + 弹幕/播放器增强）"
    else
        log "提示：当前 dashboard-ui 未注入 ext.js，仅为官方原版界面"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# 媒体库目录：飞牛共享文件夹授权 -> Emby 能看到的本机绝对路径
#   原生进程不需要挂载，直接把这些路径写进 Emby 的 library 配置即可；
#   这里只负责把可用路径汇总出来，交给 Emby 初始化时使用 + 供用户参考。
# ---------------------------------------------------------------------------
media_paths() {
    local manual="${EMBY_STATE_DIR}/media_dirs.txt"
    local raw
    if [ -f "${manual}" ]; then
        raw="$(cat "${manual}")"
    else
        raw="$(printf '%s' "${TRIM_DATA_SHARE_PATHS:-}" | tr ':' '\n')"
    fi

    local p seen=""
    while IFS= read -r p; do
        p="$(printf '%s' "${p}" | sed 's/\r$//; s/[[:space:]]*$//')"
        [ -z "${p}" ] && continue
        # 必须是绝对路径
        [ "${p#/}" = "${p}" ] && continue
        # 排除系统伪文件系统与容器内部路径：把它们交给 Emby 扫描
        # 会拖垮扫描甚至读到内核数据，也没有任何媒体意义
        case "${p}" in
            /proc|/proc/*|/sys|/sys/*|/dev|/dev/*|/run|/run/*|/tmp|/tmp/*) continue ;;
            /etc|/etc/*|/usr|/usr/*|/var|/var/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*) continue ;;
            /config|/config/*|/mnt|/mnt/*) continue ;;
        esac
        case " ${seen} " in
            *" ${p} "*) continue ;;
        esac
        seen="${seen} ${p}"
        [ -d "${p}" ] && printf '%s\n' "${p}"
    done <<EOF
${raw}
EOF
}

# 把可用媒体目录写进一份清单，Emby 首次启动与用户排查都靠它
publish_media_list() {
    local out="${EMBY_DATA}/media-paths.txt"
    {
        echo "# Emby 可见的媒体目录（由飞牛授权目录自动生成，勿手工编辑）"
        echo "# 在 Emby 后台“添加媒体库 -> 文件夹”里直接输入下面的路径"
        media_paths
    } > "${out}" 2>/dev/null || true
    local n
    n="$(media_paths | wc -l | tr -d ' ')"
    log "当前授权给 Emby 的媒体目录：${n} 个（清单：${out}）"
    return 0
}

# ---------------------------------------------------------------------------
# 进程控制
# ---------------------------------------------------------------------------
read_pid() {
    [ -f "${EMBY_PID_FILE}" ] || return 1
    local pid
    pid="$(cat "${EMBY_PID_FILE}" 2>/dev/null | tr -d '[:space:]')"
    case "${pid}" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s' "${pid}"
}

is_running() {
    local pid
    pid="$(read_pid)" || return 1
    command kill -0 "${pid}" 2>/dev/null || return 1
    # 校验 PID 确实属于 Emby，避免 PID 复用误判
    if [ -r "/proc/${pid}/cmdline" ]; then
        tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q "${EMBY_PROC}" || return 1
    fi
    return 0
}

# 等端口真正可用。
# 只在显式设置 EMBY_START_TIMEOUT（秒）时才启用 —— 默认不走这条路。
#
# 为什么默认不等：飞牛的应用中心对 start 命令有响应时间预期，官方商店里的应用
# 都是「spawn 完就返回」。若在这里阻塞几十秒等端口，应用中心会一直停在
# 「启用中」，看起来像卡死（实测踩过这个坑）。服务本身的可用性由进程存活判定，
# 用户点开图标时 Emby 若还没就绪，浏览器刷新一下即可。
wait_ready() {
    local pid="$1"
    local timeout="${EMBY_START_TIMEOUT:-}"
    case "${timeout}" in
        ''|*[!0-9]*) return 0 ;;        # 未配置 -> 不等待，直接返回成功
        0) return 0 ;;
    esac

    local i=0
    while [ "${i}" -lt "${timeout}" ]; do
        command kill -0 "${pid}" 2>/dev/null || return 1
        if (exec 3<>"/dev/tcp/127.0.0.1/${TRIM_SERVICE_PORT:-8096}") 2>/dev/null; then
            exec 3<&- 2>/dev/null || true
            return 0
        fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# 决定用哪个用户跑 Emby 本体。
#
# 真机实测结论（飞牛 fnOS / Debian 12）：**实际就是 root 运行**。
# 原因是 /vol1 的权限是 d---------（只有 root 能穿越），而应用数据目录
# /vol1/@appdata/emby 位于其下。非 root 用户即使拥有该目录的 ACL，
# 也会因为进不去父目录而无法写入 —— 降权过去等于把服务弄挂。
#
# 逻辑：只认飞牛注入的应用用户，且必须实测「能写数据目录」才降权；
# 否则保持当前身份（root）并写一条明确的日志。
# **绝不回退到 nobody**：nobody 必然写不了 programdata，症状还很隐蔽。
resolve_run_user() {
    local u
    for u in "${TRIM_USERNAME:-}" "${TRIM_RUN_USERNAME:-}"; do
        [ -n "${u}" ] || continue
        [ "${u}" = "root" ] && continue
        id "${u}" >/dev/null 2>&1 || continue

        # 先把数据目录归属交还给它（安装/升级后第一次启动可能还是 root 的）
        mkdir -p "${EMBY_DATA}" 2>/dev/null || true
        chown -R "${u}:${TRIM_GROUPNAME:-${u}}" "${EMBY_DATA}" 2>/dev/null || true
        chmod 0750 "${EMBY_DATA}" 2>/dev/null || true

        # 实测写权限。su 在部分系统上会等待密码，因此加超时兜底。
        if timeout 5 su -s /bin/sh -c "test -w '${EMBY_DATA}'" "${u}" >/dev/null 2>&1; then
            printf '%s' "${u}"
            return 0
        fi

        log "用户 ${u} 无法写入 ${EMBY_DATA}（上层目录不可穿越），改用当前身份运行"
        log "如需降权运行：请确认 ${u} 对数据目录全路径均有 x 权限，或改用 run-as=root 语义"
    done
    return 1
}

# 以脱离的方式拉起启动脚本：setsid 优先，确保生命周期脚本退出后
# 服务不会被飞牛连进程组一起收走。
spawn_emby() {
    local run_user="$1"
    local runner=""

    if [ -n "${run_user}" ]; then
        # fnOS 上没有 runuser，只有 su（且 su 不支持 -n）
        runner="$(command -v runuser 2>/dev/null || true)"
        if [ -z "${runner}" ]; then
            runner="$(command -v setpriv 2>/dev/null || true)"
        fi
        if [ -z "${runner}" ]; then
            if command -v su >/dev/null 2>&1; then
                runner="su"
            else
                log "警告：系统没有 runuser/setpriv/su，无法降权到 ${run_user}"
                run_user=""
            fi
        fi
    fi

    # setsid 让服务脱离 lifecycle 脚本的进程组，避免脚本退出时被一起收走；
    # 系统没有 setsid 时退化为普通后台进程 + nohup。
    local detach=""
    if command -v setsid >/dev/null 2>&1; then
        detach="setsid"
    else
        log "系统没有 setsid，改用 nohup 后台运行"
    fi

    if [ -n "${run_user}" ] && [ -n "${runner}" ]; then
        case "${runner}" in
            *runuser) nohup ${detach} "${runner}" -u "${run_user}" -- "${EMBY_BIN}" >>"${EMBY_LOG}" 2>&1 & ;;
            *setpriv) nohup ${detach} "${runner}" --reuid "${run_user}" --regid "${run_user}" --init-groups "${EMBY_BIN}" >>"${EMBY_LOG}" 2>&1 & ;;
            *su)      nohup ${detach} "${runner}" -s /bin/sh "${run_user}" -c "exec '${EMBY_BIN}'" >>"${EMBY_LOG}" 2>&1 & ;;
        esac
        log "以用户 ${run_user} 启动 Emby"
    else
        [ "$(id -u)" = "0" ] && log "以 root 身份启动 Emby（数据目录位于 root-only 的卷根之下）"
        # shellcheck disable=SC2086
        nohup ${detach} "${EMBY_BIN}" >>"${EMBY_LOG}" 2>&1 &
    fi
    disown 2>/dev/null || true
    return 0
}

start_emby() {
    if is_running; then
        log "Emby 已在运行（PID $(read_pid)）"
        return 0
    fi

    if [ ! -x "${EMBY_BIN}" ]; then
        log "找不到可执行启动脚本：${EMBY_BIN}"
        return 1
    fi

    log "正在启动 Emby（端口 ${TRIM_SERVICE_PORT:-8096}）"
    rm -f "${EMBY_PID_FILE}" 2>/dev/null || true

    # EmbyServer 是常驻服务，必须脱离当前会话放进后台，否则 start 调用不会返回。
    # 启动脚本会先写 PID 文件再原地等待，因此这里能很快拿到进程号。
    local run_user=""
    run_user="$(resolve_run_user)" || true
    spawn_emby "${run_user}"

    # 只等 PID 文件出现（启动脚本写得很早），不等端口就绪 ——
    # 见 wait_ready 上方注释：应用中心需要 start 尽快返回。
    local i=0 pid=""
    while [ "${i}" -lt 15 ]; do
        if pid="$(read_pid)"; then
            break
        fi
        sleep 1
        i=$((i + 1))
    done

    if [ -z "${pid}" ]; then
        log "启动失败：未生成 PID 文件，请查看日志 ${EMBY_LOG}"
        tail -n 20 "${EMBY_LOG}" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>/dev/null || true
        return 1
    fi

    if ! command kill -0 "${pid}" 2>/dev/null; then
        log "启动失败：进程已退出，日志见 ${EMBY_LOG}"
        tail -n 20 "${EMBY_LOG}" >> "${TRIM_TEMP_LOGFILE:-/dev/null}" 2>/dev/null || true
        return 1
    fi

    # 可选的就绪等待（默认关闭，仅 EMBY_START_TIMEOUT 显式开启时生效）
    if [ -n "${EMBY_START_TIMEOUT:-}" ]; then
        if wait_ready "${pid}"; then
            log "Emby 已就绪（PID ${pid}）"
        else
            log "Emby 已启动（PID ${pid}），端口尚未就绪"
        fi
    else
        log "Emby 已启动（PID ${pid}），后台继续初始化"
    fi

    # 启动后把数据目录交还给应用用户
    restore_owner
    return 0
}

stop_emby() {
    local pid
    if ! pid="$(read_pid)"; then
        log "Emby 未在运行"
        rm -f "${EMBY_PID_FILE}" 2>/dev/null || true
        return 0
    fi

    log "正在停止 Emby（PID ${pid}）"
    command kill "${pid}" 2>/dev/null || true

    local i=0
    while [ "${i}" -lt 30 ]; do
        command kill -0 "${pid}" 2>/dev/null || break
        sleep 1
        i=$((i + 1))
    done

    if command kill -0 "${pid}" 2>/dev/null; then
        log "优雅停止超时，强制结束"
        command kill -9 "${pid}" 2>/dev/null || true
        sleep 1
    fi

    rm -f "${EMBY_PID_FILE}" 2>/dev/null || true
    log "Emby 已停止"
    return 0
}
