#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 自动登录模块

复现浏览器登录流程，用账号密码自动换取控制台 JWT（Bearer Token）：

  1. 访问 oauth2/auth（带 PKCE 挑战）拿到 login_challenge
  2. 用平台 JWKS 里的 RSA 公钥（kid=public:hydra.openid.id-token）
     以 JWE(RSA-OAEP + A256GCM) 加密密码
  3. POST /iam/authn/v1/auth/nova/login 登录，拿到 oauth2 回调地址
  4. 跟随回调拿到 authorization_code
  5. 用 code + PKCE verifier 在 token 端点换 access_token（即控制台 JWT）

依赖: requests, jwcrypto
"""

import base64
import hashlib
import html
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests
from jwcrypto import jwk, jwe
from jwcrypto.common import json_encode

import credstore

# ============================================================
# 常量（均来自对平台前端代码的反编译与 OIDC 元数据）
# ============================================================
IAM_BASE = "https://iam.sensecoreapi.cn"
# 注意：授权端点必须用 platform.sensenova.cn 域名，不能直接用 OIDC 元数据里的
# signin.sensecore.cn。两者都代理到同一套 Hydra，但 CSRF cookie（oauth2_authentication_csrf）
# 的域会随入口域名走：用 signin.sensecore.cn 发起 → cookie 落在 signin.sensecore.cn，
# 而 nova/login 回调的兑换地址是 platform.sensenova.cn/oauth2/auth，跨域拿不到 cookie，
# 会报 "No CSRF value available in the session cookie"。用 platform.sensenova.cn 发起即可。
OIDC_AUTH = "https://platform.sensenova.cn/oauth2/auth"
OIDC_TOKEN = "https://signin.sensecore.cn/oauth2/token"
JWKS_URL = "https://signin.sensecore.cn/.well-known/jwks.json"
CLIENT_ID = "nova"
REDIRECT_URI = "https://platform.sensenova.cn"
SCOPE = "openid offline offline_access"

_USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")

# 缓存的公钥（首次获取后复用，避免每次登录都拉 JWKS）
_pubkey_cache = {"key": None, "ts": 0}
_PUBKEY_TTL = 3600

# 诊断日志默认关闭：只有显式设置 SENSENOVA_AUTH_DEBUG=1 才落盘，且内容脱敏。
# 日志写入用户级配置目录（不再落在 exe 同级的当前工作目录）。
_DEBUG_ENABLED = os.environ.get("SENSENOVA_AUTH_DEBUG") == "1"
_DEBUG_LOG_NAME = "auth_login_last_error.log"

# 允许跟随的重定向目标域名后缀（登录链路只应访问这些域名）
_TRUSTED_HOSTS = ("sensenova.cn", "sensecore.cn", "sensecoreapi.cn")
# 单次响应体最多读取 64KB，避免被超大响应拖垮或灌入日志
_MAX_BODY_READ = 64 * 1024
# 日志中单个响应体片段的最大长度
_BODY_SNIPPET_LIMIT = 500

# 同一凭据的最小登录间隔（秒）。/api/login 已有 HTTP 层限流，但自动重登（ensure_token）
# 和按需取明文（/api/apikey）、图像路由走的都是同一条登录链路，那些路由本身没有限流。
# 会话令牌只挡浏览器来源，挡不住本机的循环脚本——这里是凭据离开本机前的最后一道兜底。
# 直接放在这里而非 dashboard.py，是为了让所有调用方（含 401 重登）都被同一把闸管住。
MIN_LOGIN_INTERVAL = 3.0
_LOGIN_THROTTLE_MAX = 512
_login_throttle = {}          # ("login", 用户名) / ("refresh", refresh_token) -> 上次放行时间
_login_throttle_lock = threading.Lock()


def _login_throttled(key):
    """放行返回 (True, 0.0)，否则返回 (False, 还需等待的秒数)。

    /api/login 的用户名来自 HTTP 请求体、不受账号数约束，故与 _Throttle 一样做淘汰，
    否则字典可以无限增长。
    """
    now = time.monotonic()
    with _login_throttle_lock:
        if len(_login_throttle) > _LOGIN_THROTTLE_MAX:
            for k in list(_login_throttle):
                if now - _login_throttle[k] > MIN_LOGIN_INTERVAL:
                    del _login_throttle[k]
        prev = _login_throttle.get(key, -1e18)
        wait = MIN_LOGIN_INTERVAL - (now - prev)
        if wait <= 0:
            _login_throttle[key] = now
            return True, 0.0
        return False, wait



def _debug_log_path():
    try:
        return os.path.join(credstore.resolve_config_dir(), _DEBUG_LOG_NAME)
    except Exception:
        return _DEBUG_LOG_NAME


def _sanitize(text, limit=_BODY_SNIPPET_LIMIT):
    """对日志内容脱敏：遮蔽 code / challenge / token / JWT 等敏感串。"""
    if not text:
        return ""
    s = str(text)
    s = re.sub(r"(?i)\b(access_token|refresh_token|login_challenge|code_verifier|state|code)=([^&\s'\"]+)",
               r"\1=<redacted>", s)
    s = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+", "Bearer <redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", "<jwt>", s)
    return s[:limit]


def _is_trusted_url(url):
    """仅允许 https 且域名在白名单内的跳转目标。"""
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _TRUSTED_HOSTS)


def _write_debug_log(hops, redirect=None, extra=""):
    if not _DEBUG_ENABLED:
        return
    try:
        path = _debug_log_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write("# SenseNova auth login debug log（已脱敏）\n")
            f.write(f"# 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"# 调试文件路径: {path}\n\n")
            if redirect:
                f.write(f"nova/login 返回的 redirect: {_sanitize(redirect)}\n\n")
            if extra:
                f.write(f"额外信息: {_sanitize(extra)}\n\n")
            for hop in hops:
                f.write(f"--- Hop {hop.get('n')} ---\n")
                f.write(f"URL: {_sanitize(hop.get('url'), 300)}\n")
                f.write(f"Method: {hop.get('method')}\n")
                f.write(f"Status: {hop.get('status')}\n")
                f.write(f"Location header: {_sanitize(hop.get('location'), 300)}\n")
                f.write(f"Body snippet:\n{_sanitize(hop.get('body_snippet'))}\n\n")
    except Exception:
        pass


def _b64url_decode(s):
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _session():
    """禁用环境变量代理的 Session。

    登录链路上的每一个请求都不能经过中间代理：nova/login 与 token 端点带密码 JWE / code，
    JWKS 端点虽无凭据，但返回的公钥决定密码加密给谁——公钥被中间人替换等于密码直接外泄。
    """
    s = requests.Session()
    s.trust_env = False
    return s


def get_enc_pubkey():
    """获取用于加密密码的 RSA 公钥（JWKS 中的 public:hydra.openid.id-token）。"""
    now = time.time()
    if _pubkey_cache["key"] and now - _pubkey_cache["ts"] < _PUBKEY_TTL:
        return _pubkey_cache["key"]
    with _session() as s:
        resp = s.get(JWKS_URL, headers={"User-Agent": _USER_AGENT}, timeout=20)
        resp.raise_for_status()
        jwks = resp.json()
    matches = [x for x in jwks.get("keys", []) if x.get("kid") == "public:hydra.openid.id-token"]
    if not matches:
        # 平台改版 / 公钥 kid 变更 → 明确提示，而不是抛 StopIteration
        raise RuntimeError("未在平台公钥集中找到预期的加密公钥（kid=public:hydra.openid.id-token），"
                           "平台登录流程可能已变更，请更新工具")
    k = matches[0]
    # 只取 n/e 重建公钥，去掉 use/alg 约束，避免 jwcrypto 报 "Invalid usage"
    pub = jwk.JWK(kty="RSA", n=k["n"], e=k["e"])
    _pubkey_cache["key"] = pub
    _pubkey_cache["ts"] = now
    return pub


def encrypt_password(password: str) -> str:
    """用 RSA-OAEP + A256GCM 加密密码，返回紧凑型 JWE 字符串。"""
    pub = get_enc_pubkey()
    return jwe.JWE(
        password.encode("utf-8"),
        recipient=pub,
        protected=json_encode({"alg": "RSA-OAEP", "enc": "A256GCM"}),
    ).serialize(compact=True)


def _pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _follow_until(loc, session, predicate, max_hops=6, hops=None):
    """
    手动跟随重定向，直到 predicate(location) 为真，返回命中的 location。
    若 Location 头里没有 code，也尝试从响应体里解析 JS/HTML 跳转或 JSON 中的 code。
    跳转目标必须落在白名单域名内，否则立即中止，避免被重定向到第三方地址。
    hops 列表会记录每一跳供排查看用。
    """
    if hops is None:
        hops = []

    def _extract_next_from_body(text):
        """从 200 响应体里找下一个跳转目标或 code。"""
        if not text:
            return None
        # 1) 直接包含 code= 的 URL
        for m in re.finditer(r"(https?://[^\"'\s<>]+[?&]code=[^&\"'\s<>]+)", text):
            if predicate(m.group(1)):
                return m.group(1)
        # 2) HTML meta refresh
        m = re.search(r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+url=["\']([^"\']+)', text, re.I)
        if m:
            return html.unescape(m.group(1))
        # 3) JS 跳转
        for pattern in [
            r"window\.location\.replace\([\"']([^\"']+)[\"']",
            r"window\.location\.href\s*=\s*[\"']([^\"']+)[\"']",
            r"location\.href\s*=\s*[\"']([^\"']+)[\"']",
            r"window\.location\s*=\s*[\"']([^\"']+)[\"']",
        ]:
            m = re.search(pattern, text)
            if m:
                return html.unescape(m.group(1))
        return None

    for idx in range(max_hops):
        if not loc:
            break
        if predicate(loc):
            return loc
        # 拒绝跳向白名单之外的地址（含非 https）
        if not _is_trusted_url(loc):
            break
        # stream=True + 限读，避免被超大/无限响应体拖住
        with session.get(loc, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
                         allow_redirects=False, timeout=30, stream=True) as r:
            status = r.status_code
            new_loc = r.headers.get("Location")
            body = b""
            if not new_loc and status in (200, 201):
                body = r.raw.read(_MAX_BODY_READ, decode_content=True) or b""
        text = body.decode("utf-8", errors="replace") if body else ""
        hops.append({
            "n": idx + 1,
            "url": loc,
            "method": "GET",
            "status": status,
            "location": new_loc or "",
            "body_snippet": text[:2000],
        })
        # 200 响应体里可能藏着跳转
        if not new_loc and text:
            new_loc = _extract_next_from_body(text)
        loc = new_loc
    return None


def _login_once(username: str, password: str, timeout: int = 30) -> dict:
    """
    用账号密码登录一次，返回 {"access_token", "refresh_token", "expires_in"}。
    access_token 即控制台 API 所需的 Bearer JWT。
    """
    if not username or not password:
        raise ValueError("用户名和密码不能为空")

    s = requests.Session()
    # 忽略环境变量里的代理设置，避免凭据被中间代理截获
    s.trust_env = False
    s.headers.update({"User-Agent": _USER_AGENT, "Accept": "*/*"})

    # ---- 1) 拿 login_challenge（带自己的 PKCE） ----
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)
    auth_params = {
        "client_id": CLIENT_ID,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
    }
    r = s.get(OIDC_AUTH, params=auth_params, allow_redirects=False, timeout=timeout)
    loc = r.headers.get("Location")
    hops = []
    challenge_loc = _follow_until(
        loc, s, lambda u: "login_challenge=" in u, hops=hops
    )
    if not challenge_loc:
        _write_debug_log(hops, extra=f"OIDC_AUTH response Location={loc}")
        raise RuntimeError("未能获取 login_challenge（登录流程中断，平台登录页可能改版，请更新工具）")
    login_challenge = re.search(r"login_challenge=([^&]+)", challenge_loc).group(1)

    # ---- 2) 加密密码并提交登录 ----
    enc = encrypt_password(password)
    resp = s.post(
        f"{IAM_BASE}/iam/authn/v1/auth/nova/login",
        json={"username": username, "password": enc,
              "challenge": login_challenge, "is_encrypt": True},
        headers={
            "Content-Type": "application/json",
            "Origin": "https://platform.sensenova.cn",
            "Referer": "https://platform.sensenova.cn/",
        },
        timeout=timeout,
    )
    # 记录 nova/login 响应，便于排查
    hops.insert(0, {
        "n": 0,
        "url": f"{IAM_BASE}/iam/authn/v1/auth/nova/login",
        "method": "POST",
        "status": resp.status_code,
        "location": "",
        "body_snippet": resp.text[:2000],
    })
    try:
        body = resp.json()
    except Exception:
        _write_debug_log(hops, extra=f"nova/login HTTP {resp.status_code}，非 JSON 响应")
        raise RuntimeError(f"登录接口返回异常（HTTP {resp.status_code}）")
    redirect = body.get("redirect")
    if not redirect:
        # 登录失败：把后端给的错误信息透传出来
        msg = body.get("message") or body.get("error") or json.dumps(body, ensure_ascii=False)
        _write_debug_log(hops, extra=f"nova/login 未返回 redirect，msg={msg}")
        raise RuntimeError(f"登录失败：{msg}")

    # ---- 3) 跟随回调拿 authorization_code ----
    code_loc = _follow_until(redirect, s, lambda u: "code=" in u, hops=hops)
    if not code_loc:
        _write_debug_log(hops, redirect=redirect,
                         extra="已尝试从 Location 头和响应体（meta refresh/JS 跳转）中解析 code")
        raise RuntimeError("未能获取 authorization code（回调流程中断）")
    try:
        authorization_code = re.search(r"[?&]code=([^&]+)", code_loc).group(1)
    except (AttributeError, IndexError):
        _write_debug_log(hops, redirect=redirect,
                         extra=f"从 code_loc 解析 code 失败：{code_loc}")
        raise RuntimeError("未能从回调地址解析 authorization code")

    # ---- 4) 用 code + PKCE verifier 换 token ----
    token_resp = s.post(
        OIDC_TOKEN,
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "code_verifier": verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    try:
        tok = token_resp.json()
    except Exception:
        raise RuntimeError(f"token 接口返回异常（HTTP {token_resp.status_code}）")
    access_token = tok.get("access_token")
    if not access_token:
        msg = tok.get("error_description") or tok.get("error") or json.dumps(tok, ensure_ascii=False)
        raise RuntimeError(f"未返回 access_token：{msg}")

    return {
        "access_token": access_token,
        "refresh_token": tok.get("refresh_token", ""),
        "expires_in": int(tok.get("expires_in", 10800)),
    }


def login(username: str, password: str, timeout: int = 30) -> dict:
    """账号密码登录，带网络重试（最多 3 次，指数退避）。
    仅对网络/超时类异常重试；凭据错误 / 平台流程变更等不重试。"""
    # 闸门放在重试循环之外：内部网络重试是同一请求的自愈，不该各自消耗额度
    allowed, wait = _login_throttled(("login", (username or "").strip().lower()))
    if not allowed:
        raise RuntimeError(f"登录过于频繁，请 {int(wait) + 1} 秒后再试")

    last = None
    for attempt in range(3):
        try:
            return _login_once(username, password, timeout)
        except requests.exceptions.RequestException as e:
            # 连接超时 / DNS / 重置等瞬时网络问题可重试
            last = e
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        # 其它异常（RuntimeError：凭据错误 / 流程变更）直接向上抛，不重试
    raise RuntimeError(f"登录网络请求失败（已重试）：{_sanitize(str(last), 200)}")


def refresh(refresh_token: str, timeout: int = 30) -> dict:
    """用 refresh_token 换新的 access_token，返回 {"access_token","refresh_token","expires_in"}。
    平台若不支持 refresh grant 会抛 RuntimeError，由调用方降级为密码登录。"""
    if not refresh_token:
        raise ValueError("无 refresh_token")
    # 按 refresh_token 而非用户名作 key：调用方（ensure_token）未必传用户名，
    # 且 refresh_token 无法猜测，攻击者无法用它灌满字典
    allowed, wait = _login_throttled(("refresh", (refresh_token or "").strip().lower()))
    if not allowed:
        raise RuntimeError(f"登录过于频繁，请 {int(wait) + 1} 秒后再试")
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": _USER_AGENT, "Accept": "*/*"})
    resp = s.post(
        OIDC_TOKEN,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
            "scope": SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=timeout,
    )
    try:
        tok = resp.json()
    except Exception:
        raise RuntimeError(f"refresh_token 接口返回异常（HTTP {resp.status_code}）")
    if not tok.get("access_token"):
        msg = tok.get("error_description") or tok.get("error") or json.dumps(tok, ensure_ascii=False)
        raise RuntimeError(f"refresh_token 刷新失败：{msg}")
    return {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token", "") or refresh_token,
        "expires_in": int(tok.get("expires_in", 10800)),
    }


if __name__ == "__main__":
    # 简单自测：仅验证加密与 challenge 获取（不提交真实凭据）
    print("公钥可用，样例 JWE：")
    sample = encrypt_password("demo")
    print(sample[:60], "... (段数=%d)" % len(sample.split(".")))
    print("模块导入与加密链路 OK")
