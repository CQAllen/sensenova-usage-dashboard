#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 桌面应用入口

双击运行即弹出原生窗口，无需浏览器。

用法:
  python dashboard.py       # 有控制台（调试用）
  pythonw dashboard.py      # 无控制台（日常使用）

或直接双击 启动.bat

后端逻辑拆分：
  accounts.py       账号配置读写 + sanitize_error + 设置项
  sensenova_api.py  用量 / API Key 查询、解析、ensure_token
  imagegen.py       图像生成 / 编辑 / 下载缓存 + ImageApi
  auth_login.py     账号密码自动登录
  credstore.py       凭据加密存储
前端：web/index.html、web/styles.css、web/app.js
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit
from typing import Any

import auth_login
import accounts
import sensenova_api
import imagegen


def _fatal(title: str, message: str) -> None:
    """启动期致命错误提示：pythonw 无控制台时用原生弹窗，避免"闪退"看不到原因。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass
    try:
        print(f"{title}\n{message}", file=sys.stderr)
    except Exception:
        pass


try:
    import requests  # noqa: F401  （sensenova_api / imagegen 依赖，这里仅做启动期自检）
    import webview
    import jwcrypto  # noqa: F401  （auth_login 依赖，这里仅做启动期自检）
except Exception as _e:      # 缺少运行依赖：给出可执行的修复指引，而不是无声退出
    _fatal("SenseNova 用量查询 - 启动失败",
           f"缺少运行依赖：{_e}\n\n"
           "请在项目目录执行：\n"
           "    python -m pip install -r requirements.txt\n\n"
           "或直接双击 启动.bat（会自动准备运行环境）。")
    raise SystemExit(1)


# ============================================================
# 常量
# ============================================================
def _resolve_port() -> int:
    """本地服务端口：默认由系统分配（0），可用 SENSENOVA_PORT 覆盖以便排障。"""
    v = os.environ.get("SENSENOVA_PORT")
    if v:
        try:
            return int(v)
        except ValueError:
            pass
    return 0


SERVER_PORT = _resolve_port()
MAX_BODY_BYTES = 64 * 1024                  # POST 请求体上限
MAX_IMAGE_BODY_BYTES = 32 * 1024 * 1024     # 图片编辑请求体上限（最多 5 张 base64 参考图）
APP_VERSION = "1.2.0"

# 发布仓库：更新检查会查这个 repo 的 GitHub Releases 最新版
UPDATE_REPO = "CQAllen/sensenova-usage-dashboard"

# 每次启动生成的一次性会话令牌。仅用于拦截浏览器 Web 来源
# （恶意网页 / DNS rebinding / CSRF），不对同用户的本机进程提供防护。
AUTH_TOKEN = secrets.token_urlsafe(32)


class _Throttle:
    """按 key 的最小间隔限流：同一 key 在 interval 秒内只放行一次。

    会话令牌只挡浏览器来源，挡不住本机的循环脚本；这里再用最小间隔兜底，
    避免被无节制轮询 / 撞密码打爆上游接口。
    """

    def __init__(self, interval: float, max_keys: int = 512):
        self._interval = interval
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """放行返回 (True, 0.0)；否则返回 (False, 还需等待的秒数)。"""
        now = time.monotonic()
        with self._lock:
            if len(self._last) > self._max_keys:   # 防 key 无限增长
                for k in list(self._last):
                    if now - self._last[k] > self._interval:
                        del self._last[k]
            prev = self._last.get(key, -1e18)
            wait = self._interval - (now - prev)
            if wait <= 0:
                self._last[key] = now
                return True, 0.0
            return False, wait


# 前端最小刷新间隔是 60 秒，2 秒足够挡住循环轮询
_THROTTLE_USAGE = _Throttle(2.0)
# 按用户名限流，不互相阻塞；同一账号 3 秒内只允许试一次
_THROTTLE_LOGIN = _Throttle(3.0)
# /api/keys 与 /api/usage 分开计时：两者都在各自页面被访问，共用一个会被误伤
_THROTTLE_KEYS = _Throttle(2.0)
# 图像生成按账号限流：单次出图 10–30 秒且消耗付费额度。前端虽已在生成期间禁用按钮，
# 但路由本身此前没有任何兜底；按账号计时，不同账号互不阻塞
_THROTTLE_IMAGE = _Throttle(5.0)

# 运行时变量
_server_port = None
_server = None
_server_ready = threading.Event()
_server_error = None
_update_info = None       # 后台更新检查结果：{latest, url} 或 None（未检查 / 无新版 / 查询失败）


def _resource_path(name: str) -> str:
    """定位 web/ 下的前端资源：打包态从 sys._MEIPASS，源码态从脚本目录。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "web", name)


# ============================================================
# HTTP 服务器
# ============================================================
class DashboardHandler(BaseHTTPRequestHandler):
    # 不暴露 Python 版本 / 服务实现细节
    server_version = "SenseNovaDashboard"
    sys_version = ""

    def version_string(self):
        return self.server_version

    # ---------- 响应工具 ----------
    def _security_headers(self) -> dict:
        return {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            # 脚本仅允许同源 app.js（已无内联 onclick）；样式保留内联（style 属性）
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'"
            ),
        }

    def _send_json(self, code: int, obj: dict, extra_headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._security_headers().items():
            self.send_header(k, v)
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, content_type: str, replace_token: bool = False) -> None:
        """读取并返回一个前端资源文件；replace_token 时把 __AUTH_TOKEN__ 替换为会话令牌。"""
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        if replace_token:
            body = body.replace(b"__AUTH_TOKEN__", AUTH_TOKEN.encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in self._security_headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        """统一以 JSON 报错，避免 BaseHTTPRequestHandler 泄漏版本/实现信息。"""
        if code == 404:
            self._send_json(404, {"error": "not found"})
        else:
            self._send_json(code, {"error": "request rejected"})

    # ---------- 来源校验 ----------
    def _allowed_hosts(self) -> set:
        return {
            f"127.0.0.1:{_server_port}", f"localhost:{_server_port}",
            "127.0.0.1", "localhost",
        }

    def _allowed_origins(self) -> set:
        return {
            f"http://127.0.0.1:{_server_port}",
            f"http://localhost:{_server_port}",
        }

    def _check_request(self) -> tuple[int, str] | None:
        """校验通过返回 None，否则返回 (状态码, 错误信息)。"""
        # 1) Host 头白名单：拦截把域名解析到 127.0.0.1 的 DNS rebinding
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self._allowed_hosts():
            return 403, "invalid host"
        # 2) Origin 白名单：拦截跨站发起的请求（CSRF）
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in self._allowed_origins():
            return 403, "invalid origin"
        # 3) 浏览器 fetch 元数据：跨站请求一律拒绝
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() == "cross-site":
            return 403, "cross-site request blocked"
        # 4) /api/ 必须携带本次启动生成的一次性会话令牌。
        #    只认请求头，不接受 query 传参 —— 令牌不会出现在 URL（浏览器历史 / Referer）里。
        path = urlsplit(self.path).path
        if path == "/api" or path.startswith("/api/"):
            token = self.headers.get("X-Auth-Token") or ""
            if not hmac.compare_digest(token, AUTH_TOKEN):
                return 401, "unauthorized"
        return None

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        deny = self._check_request()
        if deny:
            self._send_json(deny[0], {"error": deny[1]})
            return

        path = urlsplit(self.path).path
        if path in ("/", "/index.html"):
            self._send_file(_resource_path("index.html"),
                            "text/html; charset=utf-8", replace_token=True)
        elif path == "/static/styles.css":
            self._send_file(_resource_path("styles.css"), "text/css; charset=utf-8")
        elif path == "/static/app.js":
            self._send_file(_resource_path("app.js"),
                            "application/javascript; charset=utf-8", replace_token=True)
        elif path == "/api/usage":
            allowed, wait = _THROTTLE_USAGE.check("usage")
            if not allowed:
                self._send_json(429, {"error": "请求过于频繁，请稍后再试"},
                                {"Retry-After": str(int(wait) + 1)})
                return
            try:
                result = sensenova_api.query_all_accounts()
                warnings = accounts.pop_config_warnings()
                if warnings:
                    result["warning"] = " ".join(warnings)
                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/accounts":
            # 只回传「是否存在」，绝不回传密码 / JWT 明文
            try:
                safe = [{"username": a.get("username", ""),
                         "has_password": bool(a.get("password")),
                         "has_token": bool(a.get("jwt_token"))}
                        for a in accounts.load_accounts()]
                self._send_json(200, {"accounts": safe})
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/settings":
            # 不包 try/except 的话异常会直接断连，前端表现为白屏而非报错
            try:
                self._send_json(200, {
                    "settings": accounts.load_settings(),
                    "config_dir": accounts.BASE_DIR,
                    "api_base": sensenova_api.API_BASE,
                    "port": _server_port,
                    "version": APP_VERSION,
                    "update": _update_info,
                })
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/keys":
            allowed, wait = _THROTTLE_KEYS.check("keys")
            if not allowed:
                self._send_json(429, {"error": "请求过于频繁，请稍后再试"},
                                {"Retry-After": str(int(wait) + 1)})
                return
            try:
                self._send_json(200, sensenova_api.query_all_api_keys())
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/apikey":
            # 按需返回单个 Key 的明文，仅用于界面点「显示 / 复制」
            q = parse_qs(urlsplit(self.path).query)
            try:
                value = sensenova_api.get_api_key_plain((q.get("username") or [""])[0],
                                                        (q.get("id") or [""])[0])
                self._send_json(200, {"api_key": value})
            except Exception as e:
                self._send_json(404, {"error": accounts.sanitize_error(e)})
        elif path == "/api/history":
            # 用量趋势数据——读本地 SQLite，不触网，不限流
            q = parse_qs(urlsplit(self.path).query)
            try:
                import history
                username = (q.get("username") or [""])[0] or None
                window = (q.get("window") or [""])[0] or None
                pool_name = (q.get("pool") or [""])[0] or None
                try:
                    hours = int((q.get("hours") or ["24"])[0])
                except (TypeError, ValueError):
                    self._send_json(400, {"error": "hours 参数无效"})
                    return
                if hours not in (24, 168, 720):
                    self._send_json(400, {"error": "hours 仅支持 24 / 168 / 720"})
                    return
                if window and window not in ("5h", "7d"):
                    self._send_json(400, {"error": "window 参数无效"})
                    return
                data = history.get_history(accounts.BASE_DIR, username=username,
                                           pool_name=pool_name, window=window, hours=hours)
                cutoff = history.get_last_ts(accounts.BASE_DIR)
                if username and data:
                    # 截止时间跟随所选账号，避免展示其他账号的更新时间
                    cutoff = max(p["ts"] for g in data for p in g["points"])
                self._send_json(200, {"snapshots": data, "cutoff_ts": cutoff})
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/prediction":
            # 耗尽预测——读本地 SQLite，不触网；按「积分池 × 窗口」返回
            q = parse_qs(urlsplit(self.path).query)
            try:
                import history
                username = (q.get("username") or [""])[0]
                if not username:
                    self._send_json(200, {"predictions": []})
                    return
                if not any(a.get("username") == username for a in accounts.load_accounts()):
                    self._send_json(200, {"predictions": []})
                    return
                self._send_json(200, {"predictions": history.get_predictions(accounts.BASE_DIR, username)})
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e)})
        elif path == "/api/imagedl":
            # 锚点跳转下载：返回缓存中的原始图像字节，带 Content-Disposition 触发原生保存
            q = parse_qs(urlsplit(self.path).query)
            dl_id = (q.get("id") or [""])[0]
            item = imagegen.get_cached_image(dl_id)
            if not item or not item.get("bytes"):
                self._send_json(404, {"error": "图片已过期或不存在，请重新生成"})
                return
            fmt = item.get("fmt") or "png"
            raw = item["bytes"]
            self.send_response(200)
            self.send_header("Content-Type", "image/" + fmt)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Content-Disposition",
                             'attachment; filename="sensenova_image.' + fmt + '"')
            for k, v in self._security_headers().items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        deny = self._check_request()
        if deny:
            self._send_json(deny[0], {"error": deny[1]})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        path = urlsplit(self.path).path
        # 图片编辑路由携带多张 base64 参考图，单独放宽上限
        body_limit = MAX_IMAGE_BODY_BYTES if path == "/api/imageedit" else MAX_BODY_BYTES
        if length < 0 or length > body_limit:
            # 回包前先把请求体读干净：否则 socket 上残留数据会让连接以 RST 关闭，
            # 客户端根本收不到这个 413（Windows 上表现为「连接被强制中断」）。
            # 最多读 1MB，避免被谎报超大 Content-Length 的客户端拖住线程。
            self.close_connection = True
            to_read = min(length, 1024 * 1024)
            if to_read > 0:
                self.rfile.read(to_read)
            self._send_json(413, {"success": False, "error": "请求体过大"})
            return

        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send_json(400, {"success": False, "error": "请求格式错误"})
            return
        if not isinstance(data, dict):
            self._send_json(400, {"success": False, "error": "请求格式错误"})
            return

        if path == "/api/accounts":
            try:
                accounts.merge_accounts(data.get("accounts") or [])
                self._send_json(200, {"success": True})
            except Exception as e:
                self._send_json(500, {"success": False, "error": accounts.sanitize_error(e)})

        elif path == "/api/settings":
            try:
                settings = accounts.save_settings(data)
                self._send_json(200, {"success": True, "settings": settings})
            except Exception as e:
                self._send_json(400, {"success": False, "error": accounts.sanitize_error(e)})

        elif path == "/api/clear-history":
            try:
                import history
                history.clear_all(accounts.BASE_DIR)
                self._send_json(200, {"success": True})
            except Exception as e:
                self._send_json(500, {"success": False, "error": accounts.sanitize_error(e)})

        elif path == "/api/login":
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            if not username or not password:
                self._send_json(200, {"success": False, "error": "请填写用户名和密码"})
                return
            allowed, wait = _THROTTLE_LOGIN.check(username.lower())
            if not allowed:
                self._send_json(429, {"success": False,
                                      "error": f"登录请求过于频繁，请 {int(wait) + 1} 秒后再试"},
                                {"Retry-After": str(int(wait) + 1)})
                return
            try:
                res = auth_login.login(username, password, sensenova_api.REQUEST_TIMEOUT)
                token = res.get("access_token", "")
                if not token:
                    self._send_json(200, {"success": False, "error": "登录成功但未返回 token"})
                    return
                accounts.update_account(
                    username, password=password, **sensenova_api._token_updates(res, token)
                )
                # 绝不回传 token / 密码，只回传状态
                self._send_json(200, {"success": True, "has_password": True, "has_token": True})
            except Exception as e:
                self._send_json(200, {"success": False, "error": accounts.sanitize_error(e, 200)})

        elif path == "/api/imagegen":
            username = (data.get("username") or "").strip()
            prompt = (data.get("prompt") or "").strip()
            size = (data.get("size") or "1024x1024").strip()
            model = (data.get("model") or "sensenova-u1.5-fast").strip()
            watermark = bool(data.get("watermark"))
            prompt_extend = data.get("prompt_extend")
            prompt_extend = True if prompt_extend is None else bool(prompt_extend)
            if not username:
                self._send_json(400, {"error": "请选择账号"})
                return
            if not prompt:
                self._send_json(400, {"error": "请输入提示词"})
                return
            allowed, wait = _THROTTLE_IMAGE.check(username.lower())
            if not allowed:
                self._send_json(429, {"error": f"出图过于频繁，请 {int(wait) + 1} 秒后再试"},
                                {"Retry-After": str(int(wait) + 1)})
                return
            try:
                api_key = imagegen.get_default_api_key(username)
                result = imagegen.generate_image(api_key, prompt, size, model,
                                                 watermark=watermark, prompt_extend=prompt_extend)
                result["dl_id"] = imagegen.store_image_for_download(
                    result["b64"], result["output_format"], result.get("size"))
                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e, 400)})

        elif path == "/api/imageedit":
            username = (data.get("username") or "").strip()
            prompt = (data.get("prompt") or "").strip()
            size = (data.get("size") or "auto").strip()
            model = (data.get("model") or "sensenova-u1.5-fast").strip()
            images = data.get("images") or []
            watermark = bool(data.get("watermark"))
            prompt_extend = data.get("prompt_extend")
            prompt_extend = True if prompt_extend is None else bool(prompt_extend)
            if not isinstance(images, list):
                self._send_json(400, {"error": "图片数据格式错误"})
                return
            if not username:
                self._send_json(400, {"error": "请选择账号"})
                return
            if not prompt:
                self._send_json(400, {"error": "请输入编辑指令"})
                return
            if not images:
                self._send_json(400, {"error": "请至少上传一张参考图"})
                return
            allowed, wait = _THROTTLE_IMAGE.check(username.lower())
            if not allowed:
                self._send_json(429, {"error": f"出图过于频繁，请 {int(wait) + 1} 秒后再试"},
                                {"Retry-After": str(int(wait) + 1)})
                return
            try:
                api_key = imagegen.get_default_api_key(username)
                result = imagegen.edit_image(api_key, prompt, images, size, model,
                                             watermark=watermark, prompt_extend=prompt_extend)
                result["dl_id"] = imagegen.store_image_for_download(
                    result["b64"], result["output_format"], result.get("size"))
                self._send_json(200, result)
            except Exception as e:
                self._send_json(500, {"error": accounts.sanitize_error(e, 400)})
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: Any) -> None:
        pass


# ============================================================
# 主流程
# ============================================================
# ============================================================
# 更新检查（引导下载：发现新版则在设置页提示，点按钮打开 release 页）
# ============================================================
def _parse_version(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", s or ""))


def _check_update() -> None:
    """后台查 GitHub 最新 release；比 APP_VERSION 新则缓存 {latest, url, download_url, size}。
    网络/解析失败一律静默——更新检查是锦上添花，不能影响启动。"""
    global _update_info
    try:
        url = f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "SenseNovaDashboard"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            data = json.loads(r.read().decode("utf-8"))
        latest = (data.get("tag_name") or "").lstrip("vV")
        if latest and _parse_version(latest) > _parse_version(APP_VERSION):
            info = {"latest": latest, "url": data.get("html_url") or url}
            # 找 .exe asset 的下载地址（自动更新用）
            for a in data.get("assets") or []:
                name = a.get("name", "")
                if name.endswith(".exe") and a.get("browser_download_url"):
                    info["download_url"] = a["browser_download_url"]
                    info["size"] = a.get("size")
                    info["asset_name"] = name
                    break
            _update_info = info
    except Exception:
        pass


def _start_update_check() -> None:
    threading.Thread(target=_check_update, daemon=True).start()


# ============================================================
# 主流程
# ============================================================
def start_server() -> None:
    """启动 HTTP 服务器（后台线程）。绑定失败时记录原因并唤醒等待方。"""
    global _server, _server_port, _server_error
    try:
        _server = ThreadingHTTPServer(("127.0.0.1", SERVER_PORT), DashboardHandler)
        _server_port = _server.server_address[1]
    except Exception as e:
        _server_error = accounts.sanitize_error(e)
        _server_ready.set()
        return
    # 套接字已 bind+listen，先唤醒主线程创建窗口，再进入服务循环
    _server_ready.set()
    _server.serve_forever()


class AppApi(imagegen.ImageApi):
    """前端 js_api 桥：继承 ImageApi 的下载功能，并增加窗口控制。"""
    _maximized = False

    def minimize_window(self):
        if webview.windows:
            webview.windows[0].minimize()

    def toggle_maximize_window(self):
        if not webview.windows:
            return False
        w = webview.windows[0]
        if AppApi._maximized:
            w.restore()
            AppApi._maximized = False
        else:
            w.maximize()
            AppApi._maximized = True
        return AppApi._maximized

    def close_window(self):
        if webview.windows:
            webview.windows[0].destroy()

    def download_and_update(self):
        """从 GitHub Release 下载新版 exe → 生成批处理交换 → 启动 → 退出当前进程。

        流程：下载到 %TEMP% → 生成 bat（等 PID 退出 → move → start → 自删）
        → Popen detached 启动 bat → destroy 当前窗口。源码模式不支持。
        """
        if not getattr(sys, "frozen", False):
            return {"ok": False, "error": "源码运行模式不支持自动更新，请手动下载或 git pull"}
        if not _update_info or not _update_info.get("download_url"):
            return {"ok": False, "error": "无可用的下载地址"}

        download_url = _update_info["download_url"]
        current_exe = sys.executable
        temp_dir = os.environ.get("TEMP") or os.path.expanduser("~")
        new_exe_path = os.path.join(temp_dir, "sensenova_update.exe")
        bat_path = os.path.join(temp_dir, "sensenova_updater.bat")

        # ---- 下载 ----
        try:
            resp = requests.get(download_url, stream=True, timeout=120)
            resp.raise_for_status()
            with open(new_exe_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            return {"ok": False, "error": f"下载失败: {accounts.sanitize_error(e, 120)}"}

        # ---- 校验大小 ----
        expected = _update_info.get("size")
        if expected and isinstance(expected, int) and expected > 0:
            actual = os.path.getsize(new_exe_path)
            if actual != expected:
                return {"ok": False, "error": f"下载大小不匹配（期望 {expected}，实际 {actual}）"}

        # ---- 生成批处理：等进程退出 → move → 重启 → 自删 ----
        pid = os.getpid()
        bat = (
            "@echo off\r\n"
            "chcp 65001 >nul 2>nul\r\n"
            f'set "PID={pid}"\r\n'
            f'set "NEW_EXE={new_exe_path}"\r\n'
            f'set "OLD_EXE={current_exe}"\r\n'
            "\r\n"
            ":loop\r\n"
            'tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul\r\n'
            "if errorlevel 1 goto replace\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "goto loop\r\n"
            "\r\n"
            ":replace\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            'move /Y "%NEW_EXE%" "%OLD_EXE%" 2>nul\r\n'
            "if errorlevel 1 (\r\n"
            "  timeout /t 3 /nobreak >nul\r\n"
            '  move /Y "%NEW_EXE%" "%OLD_EXE%" 2>nul\r\n'
            ")\r\n"
            'start "" "%OLD_EXE%"\r\n'
            'del "%~f0"\r\n'
        )
        try:
            with open(bat_path, "w", encoding="utf-8") as f:
                f.write(bat)
        except Exception as e:
            return {"ok": False, "error": f"生成更新脚本失败: {accounts.sanitize_error(e, 120)}"}

        # ---- 启动批处理（detached，不随父进程退出） ----
        try:
            subprocess.Popen(
                ["cmd", "/c", bat_path],
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                close_fds=True,
            )
        except Exception as e:
            return {"ok": False, "error": f"启动更新脚本失败: {accounts.sanitize_error(e, 120)}"}

        # ---- 退出当前进程，批处理接管 ----
        if webview.windows:
            webview.windows[0].destroy()
        return {"ok": True}


def main() -> None:
    # 各模块的 logger 统一落盘到配置目录：pythonw 无控制台，不写文件则告警不可见
    try:
        os.makedirs(accounts.BASE_DIR, exist_ok=True)
        logging.basicConfig(
            filename=os.path.join(accounts.BASE_DIR, "sensenova_app.log"),
            level=logging.WARNING,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    except Exception:
        pass

    # 启动后台 HTTP 服务
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # 后台检查是否有新版本（结果进 /api/settings 供界面提示）
    _start_update_check()

    # 等服务就绪（避免 --windowed 下静默挂起）
    _server_ready.wait(10)
    if _server_port is None:
        reason = _server_error or "未知错误"
        webview.create_window(
            title="SenseNova 用量查询 - 启动失败",
            html=("<div style='font-family:sans-serif;padding:16px'>"
                  f"<h3>本地服务启动失败</h3><p>{reason}</p>"
                  "<p style='color:#888'>请关闭其他占用同一端口的程序后重试。</p></div>"),
            width=440, height=240,
        )
        webview.start()
        sys.exit(1)

    url = f"http://127.0.0.1:{_server_port}"
    print(f"Server ready at {url}")

    # 创建原生窗口（无边框，自定义标题栏）
    # 宽度需保证「各账号使用情况」7 列表头不换行：7 列约 1010px + 卡片内边距 36px + 菜单 170px + 外边距 32px ≈ 1248px
    webview.create_window(
        title="SenseNova 用量查询",
        url=url,
        width=1440,
        height=860,
        min_size=(1280, 720),
        frameless=True,
        easy_drag=False,
        text_select=True,
        js_api=AppApi(),
    )

    # 启动窗口（阻塞，关闭窗口即退出程序）
    webview.start()

    # 窗口关闭后退出
    if _server:
        _server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
