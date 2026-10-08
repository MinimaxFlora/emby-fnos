# Emby for 飞牛 fnOS（原生安装包）

把 Emby 媒体服务器做成飞牛 fnOS 的**原生安装包**（`.fpk`），支持 x86_64 与 ARM64。

**这是原生软件，不是 Docker。** Emby 以普通进程直接跑在 NAS 上，由 `cmd/main` 拉起，不依赖 Docker、不占容器资源。安装方式与应用商店里其他应用完全一致：应用中心 → 手动安装 → 选 `.fpk`。

| 产物 | 架构 | 飞牛 `platform` | 来源运行时 | FPK 体积 | 解包后 |
|---|---|---|---|---|---|
| `dist/emby_4.10.1.0_x86.fpk` | x86_64 | `x86` | amd64 | 458.4 MB | 1180 MB |
| `dist/emby_4.10.1.0_arm.fpk` | ARM64 | `arm` | arm64 | 196.3 MB | 520 MB |

> ✅ **x86 包已在真实飞牛 NAS（Debian 12 / glibc 2.36）上完整验证**：安装 → 启动 → 监听端口 → HTTP 200，10 秒内就绪，13 个硬解编码器可用。

> ⚠️ 装错架构会因 ELF 格式不符直接起不来，请按 NAS 架构选包。

### 不需要本机环境？用 GitHub Actions 构建

仓库已配好工作流（`.github/workflows/build.yml`），**在 Linux runner 上构建两个架构**，
不需要 Windows、不需要 WSL、不需要 Docker：

```bash
git push                    # push 到 main 自动构建
gh workflow run "Build fnOS packages"     # 或手动触发
```

产物在 Actions 运行的 **Artifacts** 里（`fpk-x86` / `fpk-arm`，保留 30 天）。
打 `v*` 标签会自动发布到 Release。

配合 `tools/gh_artifacts.py` 可以直接把 CI 产物取回 `dist/`：

```bash
export GH_TOKEN=<你的 token>
python tools/gh_artifacts.py                    # 列出产物
python tools/gh_artifacts.py --get fpk-x86      # 下载到 dist/
```

CI 跑的是与本机完全相同的构建脚本（`build/build_payload.sh`）。
实测两条链路的产物只差 42 KB（480 MB 量级），可视为一致。

---

## 一、安装

1. 把对应架构的 `.fpk` 拷到 NAS
2. 飞牛桌面 → **应用中心** → **手动安装** → 选 `.fpk` → 选择存储空间
3. 等状态变「运行中」（首次启动约 10~30 秒）
4. 访问 `http://<NAS-IP>:8096` 完成 Emby 初始化

### 让 Emby 看到影视文件

安装后**应用中心 → Emby → 应用设置 → 授权目录**，勾选影视共享文件夹即可；也可在向导/设置里手填绝对路径。

授权后 Emby 会**自动重启**并刷新目录清单。清单文件在应用数据目录下：

```
<应用数据目录>/media-paths.txt     # Emby 当前可见的媒体目录（由授权目录生成）
<应用数据目录>/emby.log            # 运行日志，启动失败先看这里
```

在 Emby 后台「添加媒体库 → 文件夹」里填入清单中的路径即可（原生模式用的是 **NAS 真实路径**，不是容器内路径）。

### 硬件转码

程序体自带 VAAPI 驱动与 ffmpeg，启动脚本已配好搜索路径：

- **x86**：随包 5 个驱动 —— Intel `iHD`/`i965`、AMD `radeonsi`/`r600`、`libgallium`
- **ARM**：Emby 的 arm64 包**本身不带 VAAPI 驱动**（官方 deb 里没有 `lib/dri`），靠 NAS 系统提供（如 RK3588 的 `rockchip`）。启动脚本已把 `/usr/lib/dri`、`/usr/lib/aarch64-linux-gnu/dri` 等系统目录追加进 `LIBVA_DRIVERS_PATH`，否则 ARM 硬解会直接不可用
- 驱动装在别处时，可用环境变量 `EMBY_EXTRA_VA_DIRS` 追加自定义目录

**Emby 后台 → 转码 → 硬件加速** → 选 `VAAPI`，勾选需要硬解的编码即可。

排查硬解可用 `bin/vainfo`、`bin/clinfo`（均随包提供）。

---

## 二、程序体的来源与增强模块

程序体基于上游发布的 Emby 服务端运行时构建，并做了以下适配：

### 支持的媒体与播放能力

| 能力 | 说明 |
|---|---|
| 硬件转码 | VAAPI / QSV / NVENC / AMF，Intel、AMD、NVIDIA 与 ARM（RK3588 等）平台都能用；ffmpeg 内置 13 个硬解编码器 |
| 容器格式 | MKV / MP4 / TS / M2TS / AVI / MOV / FLV / WMV / RMVB 等，由内置 ffmpeg 5.1 负责解码与转封装 |
| 字幕 | SRT / ASS / SSA / PGS / VOBSUB，支持字体与样式（随包带 fontconfig 配置与字体目录） |
| 弹幕 | 内置弹幕增强模块，可直接加载 ASS/XML 弹幕 |
| 图片与元数据 | SkiaSharp + libvips 图像处理，海报/缩略图生成与刮削 |
| 外部播放器 | 提供 PotPlayer / VLC / IINA / MPV / MX Player 等外部播放器的调用入口 |
| 客户端 | 官方 Emby App、网页端、电视端、DLNA 均可连接 |

### 随包附带的界面增强

`index.html` 里多了一行 `<script data-main="ext" src="require.js">`，由此加载：

- `ede.user.js`（260 KB）— 弹幕增强
- `actorPlus.js` — 未知演员隐藏
- `embyLaunchPotplayer.js` — 外部播放器支持
- `danmaku.min.js`、`emby-crx/`（美化，需浏览器扩展）、多位播放器图标

因为 Emby 直接从程序目录提供 Web UI，**浏览器打开界面这些就自动生效**，无需额外注入。
可通过 `system/dashboard-ui/ext.js` 里的 `extmod` 数组开关附加模块。

---

## 三、库目录为什么要拆开（真机实测后才定下来的）

初版是「把上游的 `lib/` 整体带上，用 `LD_LIBRARY_PATH` 指过去」，**在真机上直接段错误**。逐个查清后改成现在的结构：

| 目录 | 放什么 | 谁用它 | 怎么用 |
|---|---|---|---|
| `system/` | EmbyServer + .NET 运行时 + Emby `dlopen` 的原生库（sqlite3 / SkiaSharp / vips / GPU 计算库） | EmbyServer | 系统加载器 `--library-path <app>/system` |
| `lib/` | **只放 ffmpeg 的库**（`libav*` / `libsw*` / `libpostproc`） | ffmpeg / ffprobe / ffdetect | 系统加载器 `--library-path <app>/lib:<app>/bin` |
| `lib/dri` | VAAPI 驱动 | Emby 转码 | `LIBVA_DRIVERS_PATH` |

**被剔除的**：随包的 `libc.so.6` / `ld-linux-*` / `libstdc++` / `libm` / `libgcc_s` / `libpthread` / `libdl` / `librt`。三条实测理由：

1. 随包 libc 是 **crosstool-NG 构建的 glibc 2.34**，比飞牛系统的 **Debian glibc 2.36 更旧**。一旦进入库搜索路径，整个进程段错误 —— 实测连 `/bin/bash`、`/usr/bin/ldd` 都崩
2. 随包的 crosstool-NG 加载器**不读 `/etc/ld.so.cache`**，连系统里的 `libavdevice` 都找不到
3. 系统库完全够用（EmbyServer 只要 GLIBC_2.16 / 2.17），带旧副本纯属添乱

---

## 四、权限模型

`config/privilege` 与官方 EmbyServer 包保持一致：**`run-as: package` + 独立应用用户 `EmbyServer`**。

| 能力 | 是否需要 root | 现状 |
|---|---|---|
| 硬件转码（`/dev/dri`） | 否（靠 `join-groups: video,render`） | 生效 |
| 写入应用数据目录（数据库、配置、缓存） | 否 | 生效 |
| 写入系统级文件（如 `/etc/hosts`） | **是** | 记一条提示后跳过，不影响启动 |

选择 `package` 的理由：官方包就是这个身份、行为可预期；而 `run-as: root` 会让
Emby 长期以 root 常驻，飞牛文档还专门提醒过 `privilege` 里同时写 `username` 会
**覆盖** `run-as: root`（1panel 踩过这个坑）。少一个 root 依赖，就少一类难查的启动失败。

所有需要写系统级文件的操作都是**幂等且允许失败**的：能写就写，写不了只记一条日志，
绝不因此让应用启动失败。

### 版本与发布是自动跟随上游的

工作流不写死版本号。`detect` 任务会**交叉验证两个来源**：

1. Emby 官方 stable release（GitHub `/releases/latest` 系列接口，自动排除 beta）
2. 上游运行时在 Docker Hub 上是否有该版本的 `-amd64` / `-arm64` tag

两边都有才采用，否则回退到上一个能对上的版本 —— 因为程序体是「官方 deb 的资源 +
上游运行时的运行时」拼起来的，缺任何一半都组装不出来。

**不需要手动打 tag**：push 到 main、每日定时（03:17 UTC）、手动触发都会走完整流程并发布。
发布用版本号作为 tag（如 `4.10.1.0`），同名 tag 已存在时会先删除再重建，
所以重复运行不会失败、也不会出现「tag 已存在」的报错。
下载地址固定为：

```
https://github.com/MinimaxFlora/emby-fnos/releases/download/<版本号>/emby_<版本号>_x86.fpk
https://github.com/MinimaxFlora/emby-fnos/releases/download/<版本号>/emby_<版本号>_arm.fpk
```
上游一发新版本，第二天的定时任务就会自动产出对应的包。

## 五、启动行为与排障（踩过的坑）

### 为什么 `start` 必须立刻返回

飞牛应用中心对 `start` 有响应时间预期。官方商店里的应用都是"spawn 完就返回"。
本包因此**默认不等端口就绪**：只等 PID 文件出现（约 1 秒），随后立即返回，
服务在后台继续初始化。

> 早先的版本会阻塞最多 30 秒等端口，结果应用中心一直停在「启用中」，
> 看起来像卡死。现在有第 6 组回归测试专门守着这条：`start_emby` 必须在
> 20 秒内返回（实测约 1 秒）。

需要"启动即就绪"的场景（例如脚本化部署）可以显式打开等待：

```bash
export EMBY_START_TIMEOUT=30      # 秒；不设置则完全不等待
```

### 出问题先看这两个文件

```
<应用数据目录>/emby.log                      # 服务日志，启动失败必看
<应用数据目录>/media-paths.txt               # Emby 当前可见的媒体目录
```

启动脚本还内置了执行轨迹钩子（正常不需要）：

```bash
EMBY_LAUNCHER_TRACE=/tmp/emby-launch.log    # 让 bin/emby-server 输出 set -x 轨迹
```

### 运行身份

真机实测的结论是 **Emby 实际以 root 运行**，原因不在脚本而在权限布局：

```
drwxr-xr-x  /vol1/@appdata
drwxrwx---+ /vol1/@appdata/emby     ← 归 EmbyServer，ACL 给足 rwx
d---------  /vol1                    ← 只有 root 能穿越！
```

数据目录在 `/vol1` 之下，而 `/vol1` 是 `d---------`。非 root 用户即使拥有该目录的完整 ACL，
也会因为**进不去父目录**而无法写入。降权过去等于把服务弄挂，所以 `resolve_run_user`
只在实测「能写数据目录」时才降权，否则保持 root 身份并写一条明确日志。

**绝不回退到 `nobody`**：nobody 必然写不了 programdata，而且症状极隐蔽（进程在、日志空白）。

### 启动方式（真机验证过的三步）

```bash
/lib64/ld-linux-x86-64.so.2 --library-path <app>/system <app>/system/EmbyServer ...
```

1. **必须显式用系统加载器**：`EmbyServer` 的 ELF `PT_INTERP` 写死 `/lib/ld-linux-x86-64.so.2`，
   而 Debian 12 用 multiarch 布局（真实路径是 `/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2`），
   该路径不存在 → 直接执行报 `No such file or directory`。**看着像文件缺失，其实是缺加载器**
2. **加载器必须是实体文件放在 apphost 同目录**：用绝对路径调用加载器时，.NET 会去
   「加载器所在目录」找 `EmbyServer.dll`，报
   `The application to execute does not exist: /usr/lib/x86_64-linux-gnu/EmbyServer.dll`。
   软链也不行（同样解析错），必须 `cp` 一份过去（仅 215 KB）
3. **绝不 `export LD_LIBRARY_PATH`**：那会影响所有子进程（实测把 `ldd`、`bash` 都带崩），
   只用 `--library-path` 把作用域限制在 EmbyServer 自身

`ffmpeg` / `ffprobe` / `ffdetect` 有同样的问题（`PT_INTERP` 缺失 + 共享链接需要 `libav*`），
所以安装时会把它们改名为 `.real` 并放一个用加载器拉起的小包装脚本。
**这一步必须在安装阶段做** —— 应用安装目录是只读卷，启动阶段写不进去（实测失败）。

### 端口对齐（应用中心改端口后打不开的原因）

Emby **没有** `-httpport` 之类的命令行参数（已从 `EmbyServer.dll` 里核实，只有
`-programdata` / `-nolocalportconfig`），端口只认 `config/system.xml` 的
`HttpServerPortNumber`。而飞牛是按 `TRIM_SERVICE_PORT` 做端口转发和桌面入口的。

两边一旦不一致，现象是「进程活着、端口也有人听，但飞牛转发那个端口没人应答」。
因此 `cmd/main start` 会在拉起服务**之前**调用 `sync_http_port` 把配置对齐。

### 图标必须是官方标识且带透明背景

真机踩过的坑：从 Emby 的 `dashboard-ui/images/icon-512x512.png` 生成图标，
该文件虽然是 RGBA，但**背景是实心黑**（角像素 `(0,0,0,255)`）。fnOS 遇到不透明图标
会自己套一层圆角方块，最终显示成「绿色圆角方块」而非官方菱形标识。

正确来源是**官方 fnOS 应用包里的 4 个图标文件**（透明背景、品牌绿 `#52B54B`），
由 `build/fetch_official_icons.py` 获取并可校验。回归测试会断言四张图都是
正方形且 alpha 最小值 ≤ 8（即真的透明）。

**不会回退到 `nobody`**：`nobody` 对应用数据目录没有写权限，一旦回退过去，
Emby 会因为写不了 programdata 而启动失败，现象还很隐蔽（进程在、日志空白）。
拿不到可用用户时直接用当前身份运行并写入告警。

### 容器时代的坑（已在原生版修掉）

| 症状 | 根因 | 处理 |
|---|---|---|
| 启动脚本"静默退出"、PID 文件不生成 | 生命脚本环境里的同名 shell 函数会遮蔽真实命令（`kill` 等），导致 `kill -0 $child` 误判子进程已死 | 统一改用 `command kill`，绕开任何同名函数 |
| 应用中心一直「启用中」 | `start` 阻塞等端口 | 默认不等待，见上 |

---

## 六、重新构建

### 依赖

| 工具 | 位置 | 说明 |
|---|---|---|
| `fnpack` 1.2.3 | `_tools/fnpack.exe` | 飞牛官方打包工具 |
| **WSL（Debian）** | 系统自带 | **必需**：负载组装必须在 Linux 内完成 |
| Python 3（WSL 内） | `/usr/bin/python3` | 仅标准库 |

> **为什么必须要 WSL**：Debian 用 soname 符号链接组织共享库
> （`libstdc++.so.6 -> libstdc++.so.6.0.32`），动态链接器按这些名字找文件。
> 而 Windows 创建符号链接需要管理员权限或开发者模式（`WinError 1314`），
> WSL 在 `/mnt/d` 上建的链接 Windows 侧又完全读不了（`WinError 1920`，
> fnpack 报 `The file cannot be accessed by the system`）。
>
> 若在 Windows 上组装，这 100 多个别名会全部丢失 —— **这正是之前 Emby 起不来的原因**。
> 现在负载在 WSL 内组装，并且把链接解引用为实体文件（Windows 可读），
> 真正的 soname 别名由随包的 `links.tsv` 在**安装时**用 `ln -s` 重建。

### 命令

```powershell
# 两个架构都打（会重新下载 deb 与镜像层，耗时较长）
powershell -ExecutionPolicy Bypass -File build\build-native.ps1

# 复用 _work 下已下载/已解包的素材，快速重打
powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -SkipFetch

# 单架构
powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -Arch arm -SkipFetch

# 换版本
powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -EmbyVersion 4.11.0.0

# 负载已在 WSL 里组装好时，只重打包
powershell -ExecutionPolicy Bypass -File build\build-native.ps1 -SkipPayloadBuild -SkipFetch
```

也可以完全绕过 PowerShell，直接在 WSL 里组装负载：

```bash
wsl -e bash -lc "cd /mnt/d/.../emby && bash build/build_payload.sh all"
```

### 流水线

```
1. [WSL] 下载并解包官方 emby-server-deb_<版本>_<arch>.deb  -> _work/deb-<arch>/
2. [WSL] 导出 amilys/embyserver:<版本>-<arch> 镜像树         -> _work/image-<arch>/
3. [WSL] build_native.py 合并两者为 app/ 负载（链接解引用为实体文件）
4. [WSL] 覆盖 app-assets/（启动脚本 + 桌面入口 + 官方图标）
5. [Win] 渲染 manifest、放 links.tsv、行尾归一化、硬断言
6. [Win] fnpack build -> dist/emby_<版本>_<arch>.fpk
```

### 校验

```bash
bash build/test-native.sh          # 57 项生命周期回归测试
python build/make_icons.py         # 重新生成图标（会写到 emby/ 与 app-assets/）
```

`provenance-<arch>.json` 记录负载的文件数、字节数与内容指纹，便于确认两个架构包来自不同运行时。

### 飞牛应用信息（与官方包对齐）

| 字段 | 值 | 说明 |
|---|---|---|
| `appname` | `emby` | 应用唯一标识，升级间不可改 |
| `maintainer` | `amilys` | **开发者**（飞牛没有独立的 developer 字段） |
| `distributor` | `MinimaxFlora` | **发布者** |
| `platform` | `x86` / `arm` | 两个架构各出一个包 |
| `service_port` | `8096` | 与 docker 镜像默认端口一致 |
| 图标 | `images/{0}.png` → `64.png` / `256.png` | 与官方包命名一致；图源是 Emby 官方 `icon-512x512.png` |
| `wizard/` | 只有 `uninstall` | **没有 install/config 向导**，所以安装过程无交互、直接进度条 |

---

## 七、包结构

```text
emby/                        ← fnpack 项目目录
├── manifest.template        ← 含 @PLATFORM@/@APPVERSION@，每次构建渲染出 manifest
├── ICON.PNG / ICON_256.PNG  ← 90x90 / 256x256，Emby 官方图标（透明背景）
├── app/                     ← 构建时生成
│   ├── system/              ← EmbyServer + Emby 程序集 + .NET 运行时 + Emby 原生依赖
│   ├── bin/                 ← emby-server 启动脚本 + ffmpeg/ffprobe/ffdetect
│   ├── lib/                 ← 只放 ffmpeg 的库（libav*/libsw*/libpostproc）+ lib/dri
│   ├── etc/ share/ licenses/
│   └── ui/                  ← 桌面入口与官方图标（64.png / 256.png）
├── cmd/                     ← 9 个生命周期脚本 + common.sh + service-setup
├── config/                  ← privilege（run-as package）/ resource（systemd-unit）/ emby.sc
└── wizard/                  ← 只有 uninstall（所以安装无交互、直接进度条）

app-assets/                  ← 手工维护、构建时覆盖到 app/ 的文件
├── bin/emby-server          ← 原生启动脚本（加载器选择、库路径、PID、日志）
└── ui/                      ← 桌面入口 config 与官方图标（64.png / 256.png）

build/
├── build_payload.sh         ← 平台中立的负载组装流水线（CI 与 WSL 共用同一个脚本）
├── build-native.ps1         ← Windows 侧主构建（纯 ASCII：PS 5.1 的编码限制，见文件头注释）
├── build_native.py          ← 合并镜像树与 deb；链接解引用为实体文件
├── split_libs.py            ← 按真机结论拆分库目录（剔除随包 libc/loader）
├── export_image_tree.py     ← 逐层导出镜像文件系统（保留符号链接）
├── extract_deb.py           ← 解包官方 deb（ar + lzma，跨平台无需 dpkg）
├── check_elf.py             ← ELF 解释器/依赖/GLIBC 静态体检
├── fetch_official_icons.py  ← 获取官方图标并校验透明背景
├── nas_ssh.py / nas_put.py  ← 真机排查与上传（paramiko）
└── test-native.sh           ← 57 项回归测试
```

---

## 八、已验证 / 未验证

**已在真实飞牛 NAS 上验证（x86_64 / Debian 12 / glibc 2.36）：**

- 完整安装链路：解包 → 复制系统加载器 → 生成转码工具包装，1673 个文件全部就位
- **启动成功并监听**：`http://+:8097/`，`GET /emby/System/Info/Public` → **200**，`T+10 秒` 就绪
- 服务信息返回 `{"ServerName":"zhao","Version":"4.10.1.0",...}`
- 端口对齐生效：`system.xml` 从 8096 改到 8097 后确实监听 8097
- 转码工具可用：`ffmpeg 5.1-emby_2026`，**13 个硬解编码器**（vaapi/qsv/nvenc/amf）
- 硬解探测执行：日志中 11 行 `ffdetect` 调用记录
- 负载内**无随包 libc / loader 残留**（`split_libs.py` 自检通过）
- 4 张图标与官方包**逐字节 sha256 一致**，alpha 均为 0–255（透明背景）

**开发机验证：**


- 逐层解包镜像，与官方 deb 做**逐文件 sha256 比对**，确认 6 个被改文件与全部增强文件
- 组装后的程序体自检：必需文件齐备、增强注入生效、**无容器专用文件残留**
- 57 项生命周期回归测试，含 PID 复用防护、媒体目录过滤、图标透明度、端口对齐、
  启动脚本库路径与参数、**start 必须在 20 秒内返回**、卸载数据保留/清除
- 两个 FPK 由官方 `fnpack 1.2.3` 成功打包，ELF 架构逐文件核对无串包
- 4 张图标与官方包逐字节 sha256 一致且带透明背景
- `wizard/` 仅含 `uninstall`，安装过程无交互

**未验证（需要你在真机确认）：**

1. **ARM64 包** —— ARM 机器没条件实测。x86 的结论（加载器、库拆分、包装脚本）都是
   架构无关的逻辑，加载器查找已覆盖 `ld-linux-aarch64.so.1` 的各条路径。
   若 ARM 上起不来，先看 `emby.log` 里的加载器选择日志
2. **`-ignore_vaapi_enabled_flag` 在部分 AMD 机型上是否该去掉** —— 镜像里是条件加上的，
   我固定加上了；若硬解异常可删掉该参数重打包
3. **卸载时 `wizard_delete_data` 是否按预期传入 `uninstall_callback`**
4. **应用中心界面上的图标显示** —— 文件层面已确认是官方标识且透明，
   但飞牛 UI 的图标缓存可能要卸载重装才刷新

**已知取舍：**

- `run-as: package`（与官方 EmbyServer 包一致）→ 以独立应用用户运行，不常驻 root。
  代价是无法写系统级文件，相关操作会记一条日志后跳过
- **Emby 实际以 root 运行**（不是设计选择，是 `/vol1` 权限布局决定的 —— 见第五节）。
  数据目录归属仍会交还应用用户
- 随包 `libc` / `loader` / `libstdc++` 已剔除，Emby 依赖系统的 glibc（飞牛 Debian 12
  的 2.36 完全满足，EmbyServer 只要 2.16/2.17）。好处是包更小、无版本冲突风险；
  代价是**不支持 glibc 低于 2.16 的老旧发行版**（对飞牛无影响）

---

## 九、来源

- 官方程序与字体/资源：[Emby.Releases](https://github.com/MediaBrowser/Emby.Releases) `emby-server-deb_4.10.1.0`
- 上游运行时：[amilys/embyserver](https://hub.docker.com/r/amilys/embyserver)
- 打包格式：[飞牛应用开放平台文档](https://developer.fnnas.com/docs/guide/)、[FNOSP/fnos-developer-skill](https://github.com/FNOSP/fnos-developer-skill)
- 原生 FPK 结构参考：[conversun/fnos-apps](https://github.com/conversun/fnos-apps)

本项目只做打包封装，不分发 Emby 二进制本身。Emby 是 Emby LLC 的商标与产品，
本仓库与 Emby LLC 无隶属关系；上游组件的获取与使用请遵循其自身的许可条款。
