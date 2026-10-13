# SenseNova 用量查询工具

商汤日日新（SenseNova，platform.sensenova.cn）多账号 Token Plan 资源包用量查询工具。  
解决「不想每天登录平台查看各账号剩余额度」的痛点：桌面原生窗口实时查看，每 5 分钟自动刷新。

---

## 功能特性

- **桌面应用**（`dashboard.py`）：双击即弹出原生窗口，采用**左侧菜单栏 + 右侧显示区**布局，分为「账号总览 / 账号明细 / 账号配置 / API Key / 图像生成 / 系统设置」六个页面；展示各账号积分池用量（5 小时 / 7 天窗口），自动刷新间隔可在「系统设置」中调整（默认 5 分钟），窗口内可直接配置账号。
- **图像生成 / 编辑**：基于商汤日日新 U1.5 模型，在「图像生成」页面用账号的 Token Plan API Key 调用文生图（图片生成）与图生图（图片编辑）。支持模型切换（Fast / Lite）、多种预设尺寸、参考图拖拽上传（编辑模式至多 5 张，第 1 张为主图），生成结果可一键下载到本地。
- **账号密码自动登录**：在「账号配置」面板填入**账号（用户名/邮箱）+ 密码**，工具自动完成 OAuth2 授权码 + PKCE 登录流程、抓取并存储 JWT（密码以 RSA-OAEP + A256GCM 加密传输，与网页端一致），**JWT 过期后自动用账号密码重登**，无需再手动从浏览器 F12 复制。
- **凭据加密落盘**：账号密码与 JWT 以 AES(Fernet) 加密后存入 `accounts.json`，密钥由 Windows DPAPI（按当前用户绑定）或 POSIX 下 0600 权限的密钥文件保护；窗口 UI 不再回显密码与 JWT。
- **多账号支持**：所有账号统一加密维护在 `accounts.json`，配置目录见下文「凭据存储位置」。
- **跨平台打包**：Windows 已打包为单文件 exe；macOS 可通过 `build_mac.sh` 一键打成 `.app`。

---

## 目录结构

```
用量查询/
├── dashboard.py              # 桌面应用主程序（pywebview 原生窗口 + 内置 HTTP 服务 + 路由）
├── accounts.py               # 账号 / 设置读写层（并发锁、合并语义、错误脱敏）
├── sensenova_api.py          # 用量与 API Key 查询、解析、JWT 自动刷新
├── imagegen.py               # 图像生成 / 编辑、下载缓存与原生保存桥（ImageApi）
├── auth_login.py             # 账号密码自动登录模块（OAuth2 授权码 + PKCE + 密码 JWE 加密）
├── credstore.py              # 凭据加密存储（Fernet 加密 + DPAPI/0600 密钥 + 原子写 + 明文迁移）
├── web/                      # 前端资源（index.html / styles.css / app.js，无内联脚本）
├── requirements.txt          # Python 依赖清单
├── .venv/                   # 运行环境（启动.bat 首次运行自动创建，已被 git 忽略）
├── 启动.bat                  # Windows 双击启动（自动准备环境，无控制台窗口）
├── 启动.sh                  # macOS / Linux 启动脚本（优先运行已打包二进制，否则源码模式）
├── build_mac.sh             # macOS 一键打包为 .app
├── app.ico                  # Windows 应用图标
├── app.icns                 # macOS 应用图标
├── version_info.txt          # Windows exe 版本信息资源
├── SenseNova用量查询.spec    # PyInstaller 打包配置（当前为 macOS .app 配置，见下文）
├── accounts.json            # 账号 / 密码 / JWT 配置（加密存储，勿提交到公开仓库）
├── accounts.key             # POSIX 下的加密密钥（0600 权限，勿提交）
├── dist/                    # 已打包成品
│   └── SenseNova用量查询.exe
└── .trae/documents/         # 项目文档（安全性加固方案等）
```

> 构建过程中产生的 `__pycache__/`、`build/` 为可重新生成的中间/缓存目录，已清理。  
> 重新打包时会自动生成，无需纳入版本管理。

---

## 快速开始

### 桌面应用（推荐日常使用）

**Windows**：双击 `启动.bat`，或运行已打包的 `dist/SenseNova用量查询.exe`（纯 exe 即可独立运行，凭据首次保存时自动写入加密配置）。

`启动.bat` 会依次尝试：项目内 `.venv` → 系统中已装好依赖的 Python → 自动创建 `.venv` 并安装 `requirements.txt`。若依赖仍不完整，它会**停在控制台显示原因并等待按键**（不会像以前那样无声闪退）。

> 手动方式：`python -m pip install -r requirements.txt` 后运行 `python dashboard.py`。

**macOS / Linux**：

```bash
chmod +x 启动.sh
./启动.sh
```

---

## 配置账号

在「账号配置」面板：

1. 填 **用户名**（平台登录用户名 / 邮箱，如 `shaobingtongzhi`）
2. 填 **密码**
3. 点 **登录** → 工具自动完成登录、抓取 JWT 并加密存储

JWT 约 3 小时过期，过期后工具会用保存的账号密码**自动重新登录**，你无需任何操作。

保存后窗口**不再回显**用户名之外的任何凭据：密码框始终为空，留空即表示沿用已保存的密码。JWT 为「只写不读」，前端拿不到明文。

> 说明：工具仅使用 JWT 认证，无需配置 AK/SK。

### 凭据存储位置

| 场景 | 目录 |
|---|---|
| Windows 打包态 | `%LOCALAPPDATA%\SenseNova用量查询\` |
| macOS 打包态 | `~/Library/Application Support/SenseNova用量查询/` |
| Linux | `$XDG_CONFIG_HOME/SenseNova用量查询/`（默认 `~/.config/...`） |
| 源码模式 | 默认脚本所在目录；若该目录位于 OneDrive / Dropbox / 坚果云等同步盘，则自动改用用户级目录 |

配置包含两个文件：

- `accounts.json`：加密信封 `{"v":1,"scheme":"dpapi|keyfile|plain","data":"<Fernet 密文>"}`，内含账号、密码与 JWT。
- `accounts.key`（仅非 Windows）：加密密钥，权限 `0600`。Windows 下密钥由 **DPAPI** 保护，不落独立文件。

> 升级提示：若目录中存在历史版本的**明文** `accounts.json`，首次读取时会自动迁移为加密格式并弹窗提示。解密失败的配置会被重命名为 `accounts.json.bad` 隔离，不会阻塞启动。

---

## 图像生成

「图像生成」页面调用商汤日日新 U1.5 模型，用**账号的 Token Plan API Key**（取该账号默认 / 首个 Key）作为 Bearer token，无需额外配置 AK/SK。

### 两种模式

- **图片生成（文生图）**：输入提示词 → 生成新图。支持 Fast（快速）与 Lite（高质量）模型，6 档预设尺寸（1:1 / 9:16 / 16:9 / 2:3 / 3:2 等）。
- **图片编辑（图生图）**：上传参考图 + 编辑指令 → 基于参考图改图。第 1 张为主图，至多 5 张参考图；尺寸可选 `auto`（自动适配主图）。

### 使用流程

1. 顶部选择账号（须已登录且该账号有 API Key）
2. 切换模式 → 选模型 / 尺寸 → 填提示词（编辑模式上传参考图）
3. 点「生成图像」/「编辑图像」，约 10–30 秒出图
4. 点「下载图片」弹出系统保存对话框，原生下载

> 提示词建议结构：`主体 + 场景/背景 + 风格 + 细节修饰`，越具体效果越好。
> 参考图上传后自动处理：单张超过 20MB 会拒绝（更大会先把整图解码进内存），
> 最长边超过 2048 时等比缩放并转 JPEG——5 张 base64 一起 POST，体积不受控会撞服务端 32MB 请求体上限。
> 生成结果在内存中缓存 1 小时供重复下载，过期需重新生成。
> API Key 列表按账号缓存 5 分钟，避免每次生成图都重新拉取。若在平台上新建 / 删除 Key，
> 需最多等 5 分钟生效，或到「API Key」页点刷新立即失效缓存。

### 涉及接口

| 用途 | 端点 |
|---|---|
| 文生图 | `POST https://token.sensenova.cn/v1/images/generations` |
| 图生图 | `POST https://token.sensenova.cn/v1/images/edits` |
| 认证 | 请求头 `Authorization: Bearer <API Key>` |

请求显式传 `watermark=false`、`response_format=b64_json`、`output_format=png`，避免官方默认值（加水印 / 返回临时 URL）的坑。

---

## 安全设计

工具在本机 `127.0.0.1` 上启随机端口的内置 HTTP 服务，并按以下方式收窄攻击面：

- **一次性会话令牌**：每次启动生成随机令牌注入页面，所有 `/api/*` 请求须携带 `X-Auth-Token`，用常量时间比较校验 → 拦截来自其他程序的盲目调用。
- **Host / Origin / Sec-Fetch-Site 校验**：拒绝非本机 Host（防 DNS rebinding）、非白名单 Origin 与跨站请求（防 CSRF）。
- **安全响应头**：`Content-Security-Policy`（`default-src 'none'`；脚本一律走外部 `app.js`，即 `script-src 'self'`，仅样式允许内联）、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
- **不回传凭据**：`GET /api/accounts` 只返回「是否已配置密码 / JWT」的布尔值，`POST /api/login` 不返回 token；前端全程无法读取明文密码与 JWT。
- **请求不走环境代理**：登录、用量查询、图像生成三类出网请求全部设 `trust_env=False`，环境变量里的 `HTTP_PROXY` / `HTTPS_PROXY` 拿不到凭据。
- **频控**：`/api/usage`、`/api/keys` 全局 2 秒一次；`/api/login` 与出图按账号（登录 3 秒、出图 5 秒）；
  超限返回 429 + `Retry-After`。会话令牌挡不住本机的循环脚本，这层是兜底。
  自动重登与按需取明文的链路本身没有 HTTP 限流，所以在 `auth_login` 内部又加了一道按凭据 3 秒的闸门，
  保证**任何**能触发登录的路径都被限住。
- **已知例外**：`GET /api/apikey` 会按需返回单个 API Key 的明文（界面「显示 / 复制」用，点按钮才取）。所有端点只认 `X-Auth-Token` 请求头，令牌不会出现在 URL 里。
- **凭据静态加密**：见上文「凭据存储位置」。
- **错误信息脱敏**：返回给界面的报错会剥离绝对路径、JWT 与超长串。

> 边界说明：会话令牌用于拦截**浏览器 Web 来源**（恶意网页 / DNS rebinding / CSRF），无法防御同一用户下的本机恶意进程（此类进程本就可读该用户的文件）。真正缩小爆炸半径的措施是「不回传凭据 + 静态加密」。

---

## 构建 / 打包

### 环境要求（Requirements）

**操作系统**
- Windows 10 / 11（桌面应用主目标）
- macOS 11+（如需打包 `.app`）
- 任意 Linux（仅支持源码运行，不提供官方打包）

**Python 解释器**
- 版本：3.8 – 3.13 任意版本均可（已验证 3.9 / 3.13）
- 环境：conda 环境、venv、或系统 Python 皆可，只要能 `pip install` 下列依赖
- 一台可联网第一次拉取依赖的机器

**Windows 必需系统组件**
- **Microsoft Edge WebView2 Runtime**：`pywebview` 在 Windows 默认使用 Edge 内核。绝大多数 Win10/11 已自带；若运行 exe 报「WebView2 未安装」，去微软官网下载 Evergreen Bootstrapper 安装即可。

**Python 依赖**

| 包 | 用途 | 何时需要 |
|---|---|---|
| `pywebview` | 原生窗口 GUI（内嵌网页 UI） | 运行 + 打包 |
| `jwcrypto` | 密码 JWE 加密（RSA-OAEP + A256GCM）、JWT 解析 | 运行 + 打包 |
| `cryptography` | 凭据 AES(Fernet) 加密与 DPAPI 以外的密钥封装（锁 `<49`） | 运行 + 打包 |
| `requests` | HTTP 请求 | 运行 + 打包 |
| `pyinstaller` | 打包成单文件 exe / .app | 仅打包 |

> 源码**未使用** PyJWT，打包时无需安装。仓库里提交的 `.spec` 是 **macOS `.app` 配置**（`BUNDLE` + `app.icns`），不是 Windows 的；Windows 用下面的命令行参数打包，并自行补 `--hidden-import`（`requests` / `jwcrypto` / `cryptography` / `auth_login` / `credstore` / `accounts` / `sensenova_api` / `imagegen`）与 `--add-data "web;web"`。

---

## 测试

```bash
python tests/smoke_test.py
```

107 项断言，覆盖加密信封读写与明文迁移、账号合并语义、HTTP 鉴权与来源校验、
凭据不回传、四处限流与 `auth_login` 登录闸门、写盘失败如实上报、图像下载缓存淘汰、
错误脱敏与 URL 白名单、API Key 缓存失效。

- **不联网**：所有上游接口被 monkeypatch，真实请求不会发出。
- **不动真实凭据**：配置目录重定向到临时目录，跑完自动清理，
  `%LOCALAPPDATA%` 下的 `accounts.json` 只读不写。
- 退出码 0 = 全通过，1 = 有失败，可直接接 CI。

### 打包步骤（Windows，通用）

任意装有上述依赖的 Python 解释器即可，命令与具体路径无关：

```bat
:: 1) 进入项目根目录（即本 README 所在目录）
cd /d <项目根目录>

:: 2) 内存紧张时设置，避免 OpenBLAS 线程分配导致子进程崩溃
set OPENBLAS_NUM_THREADS=1
set OMP_NUM_THREADS=1
set MKL_NUM_THREADS=1

:: 3) 用你的 Python 跑 PyInstaller；.spec 已内含 all 配置，无需在命令行重复指定
<PYTHON> -m PyInstaller "SenseNova用量查询.spec" --noconfirm
```

把 `<PYTHON>` 替换成你实际用的解释器绝对路径，例如：
- conda 环境：`D:\ProgramData\miniconda3\envs\pv_forecast\python.exe`
- venv：`C:\...\envs\default\Scripts\python.exe`
- 系统 Python：直接写 `python`（需已 `pip install` 上述依赖）

产物为 `dist/SenseNova用量查询.exe`（约 19MB，含 Python + 依赖 + 图标 + 版本信息），可独立分发。

### 关键注意事项

1. **打包前先移开旧 exe（重要）**  
   某些环境下「删除旧 exe」会被安全软件 / 沙箱钩子拦截，导致 PyInstaller 在写入新文件前中止、旧 exe 时间戳不变。用 rename 挪开即可避免：
   ```bat
   move "dist\SenseNova用量查询.exe" "_old.exe"
   ```
   再执行上面的打包命令。

2. **`.spec` 不能直接打 Windows exe**：仓库里那份是 macOS 配置（含 `BUNDLE` 段与 `app.icns`）。Windows 请用第 3 步的命令行参数；若要单独维护一份 Windows `.spec`，复制后删掉 `BUNDLE`/`COLLECT` 段、`icon` 换成 `app.ico`、加 `version='version_info.txt'` 即可。版本号统一由 `dashboard.py` 的 `APP_VERSION` 定义，改动时同步 `version_info.txt` 与 `build_mac.sh` 的 `APP_VERSION`。

3. **macOS 打包必须在本机**（PyInstaller 不支持交叉编译），用 `build_mac.sh`。需 **framework 版 Python**（如 Homebrew 的 `python3`）；系统自带 `python3` 不行（pywebview 的 Cocoa 后端依赖 framework 版）。脚本会做 ad-hoc 签名（不再写入配置模板，凭据统一落在用户级目录）。

### 一键安装依赖（参考）

```bat
<PYTHON> -m pip install -r requirements.txt pyinstaller
```

---

## 依赖

- **运行 / 打包均需**：`requests`、`pywebview`、`jwcrypto`（密码 JWE 加密 + JWT 解析）、`cryptography`（凭据加密，锁 `<49`）
- **仅打包需要**：`pyinstaller`（Windows）；`pyinstaller` + `pyobjc-core` / `pyobjc-framework-Cocoa` / `pyobjc-framework-WebKit`（macOS）
- 源码未使用 PyJWT，无需安装

---

## API

- 接口：`GET https://platform.sensenova.cn/lite/console/v1/tokenplan/pool-usage`
- 认证：请求头 `Authorization: Bearer <JWT>`
- 返回：各积分池的 `name`、`model_ids`、`window_5h` / `window_7d`（含 `limit` / `used` / `remaining` / `reset_at`）、`grant_balance`、`nearest_grant_expiry`、`pool_type`（default / dedicated）
