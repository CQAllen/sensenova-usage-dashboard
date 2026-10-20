# SenseNova 用量查询工具

商汤日日新（SenseNova，platform.sensenova.cn）多账号 Token Plan 资源包用量查询工具。  
解决「不想每天登录平台查看各账号剩余额度」的痛点：桌面原生窗口实时查看，每 5 分钟自动刷新。

---

## 功能特性

- **桌面应用**（`dashboard.py`）：双击即弹出原生窗口，采用**无边框自定义标题栏 + 左侧菜单栏 + 右侧显示区**布局，分为「账号总览 / 账号明细 / 账号管理 / 图像生成 / 系统设置」五个页面；展示各账号积分池用量（5 小时 / 7 天窗口），自动刷新间隔可在「系统设置」中调整（默认 5 分钟）。
- **账号管理（整合配置 + API Key）**：每个账号一张可展开卡片，折叠态显示登录状态徽标（已登录 / 需登录 / JWT 过期 / 未配置）与 Key 数量，展开后可登录、查看 / 复制 API Key、删除账号。
- **账号可用性分类**：账号总览按积分池额度将账号分为「可用 / 降级 / 不可用」——通用积分池有额度即可用，耗尽后回退活动固定积分，再耗尽看 Flash-Lite 专属池。
- **图像生成 / 编辑**：基于商汤日日新 U1.5 模型，用账号的 Token Plan API Key 调用文生图与图生图。支持模型切换（Fast / Lite）、多种预设尺寸、参考图拖拽上传（编辑模式至多 5 张，第 1 张为主图，自动缩放到 2048 转 JPEG），生成结果可一键下载到本地。
- **账号密码自动登录**：填入**账号 + 密码**，工具自动完成 OAuth2 授权码 + PKCE 登录流程、抓取并存储 JWT（密码以 RSA-OAEP + A256GCM 加密传输），**JWT 过期后自动用账号密码重登**，无需手动从浏览器 F12 复制。
- **自动更新**：启动时后台检查 GitHub Release，发现新版后在「系统设置」页提示，点「立即更新」自动下载 → 交换 exe → 重启，全程无需打开浏览器。
- **凭据加密落盘**：账号密码与 JWT 以 AES(Fernet) 加密后存入 `accounts.json`，密钥由 Windows DPAPI 或 POSIX 0600 权限密钥文件保护；窗口 UI 不回显密码与 JWT。
- **跨平台打包**：Windows 一键打包为单文件 exe（`build_win.py`）；macOS 一键打成 `.app`（`build_mac.sh`）；GitHub Actions 打 tag 自动发版。

---

## 目录结构

```
用量查询/
├── dashboard.py              # 桌面应用主程序（pywebview 无边框窗口 + 内置 HTTP 服务 + 路由 + 自动更新）
├── accounts.py               # 账号 / 设置读写层（并发锁、合并语义、错误脱敏）
├── sensenova_api.py          # 用量与 API Key 查询、解析、JWT 自动刷新、Key 缓存
├── imagegen.py               # 图像生成 / 编辑、下载缓存与原生保存桥（ImageApi）
├── auth_login.py             # 账号密码自动登录（OAuth2 + PKCE + 密码 JWE + 登录限流）
├── credstore.py              # 凭据加密存储（Fernet + DPAPI/0600 + 原子写 + 明文迁移）
├── web/                      # 前端资源（index.html / styles.css / app.js，CSS 设计令牌）
├── tests/smoke_test.py       # 冒烟测试（107 项，不联网、不动真实凭据）
├── requirements.txt          # Python 依赖清单
├── .venv/                   # 运行环境（启动.bat 首次运行自动创建，已被 git 忽略）
├── 启动.bat                  # Windows 双击启动（自动准备环境，无控制台窗口）
├── 启动.sh                  # macOS / Linux 启动脚本
├── build_win.py             # Windows 一键打包（读 APP_VERSION → 生成 version_info.txt → 调 sensenova_win.spec）
├── build_mac.sh             # macOS 一键打包为 .app
├── sensenova_win.spec       # Windows PyInstaller onefile 配置
├── SenseNova用量查询.spec    # macOS PyInstaller .app 配置
├── .github/workflows/release.yml  # GitHub Actions：打 tag 自动打包 + 发版
├── app.ico / app.icns       # 应用图标
├── version_info.txt          # Windows exe 版本信息（由 build_win.py 自动生成）
├── accounts.json            # 账号 / 密码 / JWT 配置（加密存储，勿提交）
├── accounts.key             # POSIX 下的加密密钥（0600 权限，勿提交）
├── dist/                    # 已打包成品
└── .trae/documents/         # 项目文档（安全性加固方案等）
```

---

## 快速开始

### 桌面应用（推荐日常使用）

**Windows**：双击 `启动.bat`，或运行已打包的 `dist/SenseNova用量查询.exe`（纯 exe 即可独立运行，凭据首次保存时自动写入加密配置）。

`启动.bat` 会依次尝试：项目内 `.venv` → 系统中已装好依赖的 Python → 自动创建 `.venv` 并安装 `requirements.txt`。若依赖仍不完整，它会**停在控制台显示原因并等待按键**。

> 手动方式：`python -m pip install -r requirements.txt` 后运行 `python dashboard.py`。

**macOS / Linux**：

```bash
chmod +x 启动.sh
./启动.sh
```

---

## 账号管理

在「账号管理」页面，每个账号是一张可展开卡片：

1. 点 **+ 添加账号** → 新卡片展开
2. 填 **用户名** + **密码**
3. 点 **登录** → 工具自动完成登录、抓取 JWT 并加密存储
4. 点卡片头部展开 → 可查看该账号的 API Key 列表

JWT 约 3 小时过期，过期后工具会用保存的账号密码**自动重新登录**。保存后窗口不回显密码，密码框留空即沿用已保存的密码。

**状态徽标**（基于积分池额度和登录态）：

| 徽标 | 含义 |
|---|---|
| 已登录 | 有密码 + 有 JWT + Key 查询正常 |
| 需登录 | 有密码但无 JWT |
| JWT 过期 | Key 查询返回过期 / 未配置错误 |
| 登录失败 | Key 查询返回其他错误 |
| 未配置 | 无密码 |

### 凭据存储位置

| 场景 | 目录 |
|---|---|
| Windows 打包态 | `%LOCALAPPDATA%\SenseNova用量查询\` |
| macOS 打包态 | `~/Library/Application Support/SenseNova用量查询/` |
| Linux | `$XDG_CONFIG_HOME/SenseNova用量查询/`（默认 `~/.config/...`） |
| 源码模式 | 默认脚本所在目录；若位于 OneDrive / Dropbox / 坚果云等同步盘，自动改用用户级目录 |

配置包含两个文件：

- `accounts.json`：加密信封 `{"v":1,"scheme":"dpapi|keyfile|plain","data":"<Fernet 密文>"}`，内含账号、密码与 JWT。
- `accounts.key`（仅非 Windows）：加密密钥，权限 `0600`。Windows 下密钥由 **DPAPI** 保护。

> 升级提示：若目录中存在历史版本的**明文** `accounts.json`，首次读取时自动迁移为加密格式并提示。解密失败的配置会被重命名为 `.bad` 隔离，不阻塞启动。

---

## 图像生成

「图像生成」页面调用商汤日日新 U1.5 模型，用**账号的 Token Plan API Key**（取默认 / 首个 Key）作为 Bearer token。

### 两种模式

- **图片生成（文生图）**：输入提示词 → 生成新图。支持 Fast / Lite 模型，6 档预设尺寸。
- **图片编辑（图生图）**：上传参考图 + 编辑指令 → 基于参考图改图。第 1 张为主图，至多 5 张。

### 使用流程

1. 顶部选择账号（须已登录且有 API Key）
2. 切换模式 → 选模型 / 尺寸 → 填提示词（编辑模式上传参考图）
3. 点「生成图像」/「编辑图像」，约 10–30 秒出图
4. 点「下载图片」弹出系统保存对话框，原生下载

> 提示词建议：`主体 + 场景/背景 + 风格 + 细节修饰`，越具体效果越好。
> 参考图上传后自动处理：单张超 20MB 拒绝，最长边超 2048 等比缩放转 JPEG。
> 生成结果在内存中缓存 1 小时供重复下载。
> API Key 列表按账号缓存 5 分钟，到「账号管理」页点刷新立即失效缓存。

### 涉及接口

| 用途 | 端点 |
|---|---|
| 文生图 | `POST https://token.sensenova.cn/v1/images/generations` |
| 图生图 | `POST https://token.sensenova.cn/v1/images/edits` |
| 认证 | 请求头 `Authorization: Bearer <API Key>` |

---

## 自动更新

启动时后台检查 GitHub Release（`https://github.com/CQAllen/sensenova-usage-dashboard/releases`），发现新版后在「系统设置」页显示「发现新版本 vX.X.X」，点「立即更新」：

1. 从 Release 下载新版 exe 到 `%TEMP%`
2. 生成批处理脚本：等当前进程退出 → `move` 覆盖旧 exe → `start` 重启 → 自删
3. 启动批处理（detached）→ 退出当前窗口
4. 批处理接管：1-2 秒内完成交换并重启

整个过程约 3-5 秒（下载取决于网速），窗口闪一下即恢复。源码运行模式不支持自动更新，提示 `git pull`。

---

## 安全设计

工具在本机 `127.0.0.1` 上启随机端口的内置 HTTP 服务，按以下方式收窄攻击面：

- **一次性会话令牌**：每次启动生成随机令牌注入页面，所有 `/api/*` 请求须携带 `X-Auth-Token`，常量时间比较校验。
- **Host / Origin / Sec-Fetch-Site 校验**：拒绝非本机 Host（防 DNS rebinding）、非白名单 Origin 与跨站请求（防 CSRF）。
- **安全响应头**：`Content-Security-Policy`（`default-src 'none'`；`script-src 'self'`，零内联脚本）、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
- **不回传凭据**：`GET /api/accounts` 只返回布尔值，`POST /api/login` 不返回 token；前端全程无法读取明文密码与 JWT。
- **请求不走环境代理**：登录、用量查询、图像生成出网请求全部设 `trust_env=False`。
- **频控**：`/api/usage`、`/api/keys` 全局 2 秒；`/api/login` 与出图按账号（登录 3 秒、出图 5 秒）；`auth_login` 内部按凭据 3 秒闸门，覆盖自动重登 / 取明文 / 出图链路。超限返回 429 + `Retry-After`。
- **凭据静态加密**：见上文「凭据存储位置」。
- **错误信息脱敏**：返回给界面的报错剥离绝对路径、JWT 与超长串。

> 边界说明：会话令牌拦截浏览器 Web 来源，无法防御同一用户下的本机恶意进程。真正缩小爆炸半径的是「不回传凭据 + 静态加密」。

---

## 构建 / 打包

### 环境要求

**操作系统**：Windows 10/11（主目标）、macOS 11+（`.app` 打包）、任意 Linux（仅源码运行）

**Python**：3.8 – 3.14 任意版本均可

**Windows 必需系统组件**：Microsoft Edge WebView2 Runtime（Win10/11 通常已自带）

**Python 依赖**

| 包 | 用途 | 何时需要 |
|---|---|---|
| `pywebview` | 原生窗口 GUI（内嵌网页 UI） | 运行 + 打包 |
| `jwcrypto` | 密码 JWE 加密 + JWT 解析 | 运行 + 打包 |
| `cryptography` | 凭据 AES(Fernet) 加密 | 运行 + 打包 |
| `requests` | HTTP 请求 | 运行 + 打包 |
| `pyinstaller` | 打包成 exe / .app | 仅打包 |

### Windows 打包

```bat
python -m pip install -r requirements.txt pyinstaller
python build_win.py
```

`build_win.py` 从 `dashboard.py` 读 `APP_VERSION`，自动生成 `version_info.txt`，调用 `sensenova_win.spec` 打包。产物为 `dist/SenseNova用量查询.exe`（单文件，双击即运行）。

> 打包前若旧 exe 正在运行，先关闭或挪开（`move "dist\SenseNova用量查询.exe" "_old.exe"`），避免文件锁。

### macOS 打包

```bash
chmod +x build_mac.sh
./build_mac.sh
```

需 **framework 版 Python**（如 Homebrew 的 `python3`）。脚本做 ad-hoc 签名，产物为 `dist/SenseNova用量查询.app`。

### GitHub Actions 自动发版

打 tag 即触发 `.github/workflows/release.yml`：

```bash
git tag v1.3.0
git push origin v1.3.0
```

Workflow 在 Windows + macOS runner 上分别打包 → 上传 artifact → 统一创建 GitHub Release 并挂载 exe / zip 产物。用户旧版本启动后会检测到新版并提示自动更新。

---

## 测试

```bash
python tests/smoke_test.py
```

107 项断言，覆盖加密信封读写与明文迁移、账号合并语义、HTTP 鉴权与来源校验、凭据不回传、四处限流与登录闸门、写盘失败如实上报、图像下载缓存淘汰、错误脱敏与 URL 白名单、API Key 缓存失效。

- **不联网**：所有上游接口被 monkeypatch。
- **不动真实凭据**：配置目录重定向到临时目录，跑完自动清理。
- 退出码 0 = 全通过，1 = 有失败，可直接接 CI。

---

## API

- 接口：`GET https://platform.sensenova.cn/lite/console/v1/tokenplan/pool-usage`
- 认证：请求头 `Authorization: Bearer <JWT>`
- 返回：各积分池的 `name`、`model_ids`、`window_5h` / `window_7d`（含 `limit` / `used` / `remaining` / `reset_at`）、`grant_balance`、`nearest_grant_expiry`、`pool_type`（default / dedicated）
