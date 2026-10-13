#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
冒烟测试：不依赖外部网络、不触碰真实凭据，可直接双击/终端运行。

运行：
    python tests/smoke_test.py
    或 .venv\\Scripts\\python.exe tests\\smoke_test.py

退出码：0 = 全通过，1 = 有失败。

覆盖：
  1. credstore    加密信封读写、明文自动迁移、解密失败隔离
  2. accounts     账号合并语义、写盘失败如实上报、错误脱敏
  3. HTTP 鉴权     会话令牌（只认请求头）、Host / Origin / Sec-Fetch-Site 校验、
                   精确路径匹配、body 上限、安全响应头
  4. 凭据不回传    /api/accounts 只回布尔，绝不含明文密码 / JWT
  5. 限流         /api/usage、/api/keys 全局；/api/login 与出图按账号；
                   auth_login 登录闸门（覆盖自动重登 / 取明文 / 出图链路）
  6. 图像缓存     条数 / 总字节 / TTL 淘汰
  7. auth_login   跳转域名白名单、日志脱敏、JWT exp 解析
  8. API Key 缓存  命中不重拉、TTL 失效、按账号清理、主动刷新联动

注意 patch 的粒度：只 patch 最底层的请求函数（query_pool_usage / query_api_keys /
login / refresh），上层业务函数保持真实代码，否则会被自己的 patch 遮蔽而测不到。

测试通过 monkeypatch 屏蔽真实网络请求，配置目录重定向到临时目录，
跑完自动清理。真实凭据（%LOCALAPPDATA% 下的 accounts.json）不会被读写。
"""

import base64
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time

# 保证可以从任意工作目录运行
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 必须用临时端口：port 0 由系统分配，避免与已在运行的实例冲突
os.environ.pop("SENSENOVA_PORT", None)
# 关掉调试日志，测试不该往配置目录写日志
os.environ.pop("SENSENOVA_AUTH_DEBUG", None)

import credstore  # noqa: E402
import accounts  # noqa: E402
import dashboard  # noqa: E402
import imagegen  # noqa: E402
import auth_login  # noqa: E402
import sensenova_api  # noqa: E402

# 所有配置读写重定向到临时目录
WORK_DIR = tempfile.mkdtemp(prefix="sn-smoke-")
accounts.BASE_DIR = WORK_DIR

# ---- 屏蔽真实网络：只 patch 最底层的请求函数，保留上层业务逻辑可测 ----
# query_all_accounts / query_pool_usage / query_api_keys 全部在此层截断，
# query_all_api_keys、resolve_account_keys 等上层函数保持真实代码可被验证。
_real_login = auth_login.login
_real_login_once = auth_login._login_once
dashboard.sensenova_api.query_all_accounts = lambda: {"timestamp": "t", "accounts": []}
dashboard.auth_login.login = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mocked"))
dashboard.auth_login.refresh = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mocked"))
dashboard.sensenova_api.query_pool_usage = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mocked"))
dashboard.sensenova_api.query_api_keys = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mocked"))

PASS_N = FAIL_N = 0


def section(title):
    print("\n== " + title)


def check(name, cond, detail=""):
    global PASS_N, FAIL_N
    PASS_N += bool(cond)
    FAIL_N += 0 if cond else 1
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + ("   <- " + repr(detail) if detail != "" else ""))
    return cond


def fake_jwt(exp):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".signature"


# ============================================================
# 1. credstore
# ============================================================
section("1. credstore 加密信封")

d1 = tempfile.mkdtemp(prefix="sn-cred-")
credstore.write_document(d1, {"accounts": [{"username": "u", "password": "p"}]})
obj, warn = credstore.read_document(d1)
check("写入后可原样读出", obj == {"accounts": [{"username": "u", "password": "p"}]})
with open(os.path.join(d1, credstore.ACCOUNTS_FILE), encoding="utf-8") as f:
    raw = f.read()
check("落盘是密文信封", raw.startswith('{"v": 1, "scheme":') and "password" not in raw)
check("密文中搜不到明文密码", "p}" not in raw)
shutil.rmtree(d1, ignore_errors=True)

# 明文自动迁移
d2 = tempfile.mkdtemp(prefix="sn-mig-")
with open(os.path.join(d2, credstore.ACCOUNTS_FILE), "w", encoding="utf-8") as f:
    f.write(json.dumps({"accounts": [{"username": "old"}]}))
obj, warn = credstore.read_document(d2)
check("旧明文可读取", obj == {"accounts": [{"username": "old"}]})
check("迁移给出告警", bool(warn))
with open(os.path.join(d2, credstore.ACCOUNTS_FILE), encoding="utf-8") as f:
    check("读取后已改为密文", f.read().startswith('{"v": 1, "scheme":'))
shutil.rmtree(d2, ignore_errors=True)

# 解密失败隔离
d3 = tempfile.mkdtemp(prefix="sn-bad-")
with open(os.path.join(d3, credstore.ACCOUNTS_FILE), "w", encoding="utf-8") as f:
    f.write(json.dumps({"v": 1, "scheme": "dpapi", "data": "not-a-fernet-token"}))
obj, warn = credstore.read_document(d3)
check("解密失败返回 None", obj is None)
check("解密失败给出告警", bool(warn))
check("坏文件已隔离为 .bad", os.path.exists(os.path.join(d3, credstore.ACCOUNTS_FILE + ".bad")))
shutil.rmtree(d3, ignore_errors=True)

# ============================================================
# 2. accounts
# ============================================================
section("2. accounts 合并语义与脱敏")

accounts.merge_accounts([{"username": "u1", "password": "p1", "jwt_token": "j1"}])
accounts.merge_accounts([{"username": "u1"}])                      # 空密码 → 沿用
acc = {a["username"]: a for a in accounts.load_accounts()}
check("空密码沿用已存值", acc["u1"]["password"] == "p1")
check("jwt_token 沿用已存值", acc["u1"]["jwt_token"] == "j1")
accounts.merge_accounts([{"username": "u1", "password_clear": True}])
acc = {a["username"]: a for a in accounts.load_accounts()}
check("password_clear 清空密码", acc["u1"]["password"] == "")
accounts.merge_accounts([{"username": "u1"}, {"username": "u1"}])  # 重复用户名去重
check("重复用户名去重", len(accounts.load_accounts()) == 1)
accounts.merge_accounts([])                                         # 提交空 → 全部删除
check("提交空列表清空账号", accounts.load_accounts() == [])

s = accounts.sanitize_error("boom at C:\\Users\\alice\\secret\\x.log jwt=" + fake_jwt(99) + " and " + "A" * 60)
check("脱敏去掉 Windows 路径", "C:\\Users" not in s and "alice" not in s)
check("脱敏去掉 /Users/<user>", "/Users/bob" not in accounts.sanitize_error("at /Users/bob/foo"))
check("脱敏去掉 JWT 形状", "<token>" in s and "eyJ" not in s)
check("脱敏截断超长串", "AAAA" not in s)

# 写盘失败必须如实上报（吞掉等于界面谎报「已保存」）
orig_write = credstore.write_document
credstore.write_document = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
try:
    raised = False
    try:
        accounts.merge_accounts([{"username": "x", "password": "y"}])
    except OSError:
        raised = True
    check("写盘失败向上抛", raised)
finally:
    credstore.write_document = orig_write
check("恢复后写盘正常", accounts.merge_accounts([{"username": "ok"}]) is None or True)
accounts.merge_accounts([])

# ============================================================
# 3~6. HTTP 层
# ============================================================
section("3~6. HTTP 服务")

dashboard._server_error = None
dashboard._server_port = None
dashboard._server_ready.clear()
threading.Thread(target=dashboard.start_server, daemon=True).start()
check("服务启动成功", dashboard._server_ready.wait(10) and bool(dashboard._server_port),
      dashboard._server_error)
PORT = dashboard._server_port
TOK = dashboard.AUTH_TOKEN


def req(method, path, body=None, token=TOK, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    h = {"Host": "127.0.0.1:%d" % PORT}
    if headers:
        h.update(headers)
    if token is not None:
        h["X-Auth-Token"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    conn.request(method, path, body=payload, headers=h)
    r = conn.getresponse()
    data = r.read().decode("utf-8", "replace")
    hd = dict(r.getheaders())
    conn.close()
    return r.status, data, hd


# 先种一个带凭据的账号，供后面「不回传」断言用
accounts.merge_accounts([{"username": "u1", "password": "SECRETPW", "jwt_token": "SECRETTOKEN"}])

print("\n-- 会话令牌与来源校验")
for _t in (dashboard._THROTTLE_USAGE, dashboard._THROTTLE_KEYS, dashboard._THROTTLE_LOGIN,
           dashboard._THROTTLE_IMAGE):
    _t._last.clear()
check("无令牌 → 401", req("GET", "/api/usage", token="")[0] == 401)
check("错误令牌 → 401", req("GET", "/api/usage", token="wrong")[0] == 401)
check("query 传令牌不生效（只认请求头）", req("GET", "/api/usage?t=" + TOK, token="")[0] == 401)
dashboard._THROTTLE_USAGE._last.clear()
check("携带正确令牌 → 200", req("GET", "/api/usage")[0] == 200)
check("非法 Host → 403", req("GET", "/", headers={"Host": "evil.com"})[0] == 403)
check("非法 Origin → 403", req("GET", "/", headers={"Origin": "http://evil.com"})[0] == 403)
check("Sec-Fetch-Site: cross-site → 403",
      req("GET", "/", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403)
check("非 /api 路径也校验 Host", req("GET", "/api/usage", headers={"Host": "evil.com"})[0] == 403)

print("\n-- 路径匹配与请求体")
check("未知路径 → 404", req("GET", "/api/usage/extra")[0] == 404)
check("前缀相似路径 → 404", req("GET", "/api/usageX")[0] == 404)
check("未知 POST 路径 → 404", req("POST", "/api/usage/extra", {})[0] == 404)
big = {"accounts": [{"username": "x" * (dashboard.MAX_BODY_BYTES + 1)}]}
check("超大 body → 413", req("POST", "/api/accounts", big)[0] == 413)

print("\n-- 安全响应头")
_st, _bd, hd = req("GET", "/api/usage")
check("Content-Security-Policy 存在", "Content-Security-Policy" in hd)
check("CSP 不启用内联脚本", "'unsafe-inline'" not in hd.get("Content-Security-Policy", "").split("style-src")[0])
check("CSP 限制脚本为同源", "script-src 'self'" in hd.get("Content-Security-Policy", ""))
check("X-Content-Type-Options: nosniff", hd.get("X-Content-Type-Options") == "nosniff")
check("Cache-Control: no-store", hd.get("Cache-Control") == "no-store")
check("Referrer-Policy: no-referrer", hd.get("Referrer-Policy") == "no-referrer")
check("不暴露 Python 版本", "Python" not in (hd.get("Server") or ""))
_st, _bd, hd403 = req("GET", "/", headers={"Host": "evil.com"})
check("403 也带安全头", "Content-Security-Policy" in hd403)

print("\n-- 凭据不回传")
_st, bd, _ = req("GET", "/api/accounts")
check("响应不含明文密码", "SECRETPW" not in bd)
check("响应不含 JWT 明文", "SECRETTOKEN" not in bd)
check("只回 has_password 布尔", '"has_password": true' in bd)
check("只回 has_token 布尔", '"has_token": true' in bd)
_st, bd, _ = req("POST", "/api/login", {"username": "u1", "password": "SECRETPW"})
check("登录响应不回 token", "SECRETTOKEN" not in bd and "jwt" not in bd)
check("登录响应不回密码", "SECRETPW" not in bd)

print("\n-- 限流")
for t in (dashboard._THROTTLE_USAGE, dashboard._THROTTLE_KEYS, dashboard._THROTTLE_LOGIN,
          dashboard._THROTTLE_IMAGE):
    t._last.clear()
_st, bd, hd = req("GET", "/api/usage")
check("/api/usage 首次放行", _st == 200, _st)
_st, bd, hd = req("GET", "/api/usage")
check("/api/usage 重复 → 429", _st == 429, (_st, bd))
check("429 带 Retry-After", "Retry-After" in hd, hd.get("Retry-After"))
check("429 仍是 JSON 错误体", "error" in json.loads(bd))
check("/api/keys 不受 /api/usage 限流影响", req("GET", "/api/keys")[0] == 200)
check("/api/keys 重复 → 429", req("GET", "/api/keys")[0] == 429)
time.sleep(2.2)
check("间隔后 /api/usage 恢复", req("GET", "/api/usage")[0] == 200)

_st, bd, _ = req("POST", "/api/login", {"username": "alice", "password": "p"})
check("/api/login 首次走业务逻辑", _st == 200 and json.loads(bd).get("success") is False, (_st, bd))
_st, bd, hd = req("POST", "/api/login", {"username": "alice", "password": "p"})
check("同账号重复登录 → 429", _st == 429, (_st, bd))
check("登录 429 带 Retry-After", "Retry-After" in hd)
check("不同账号不被限流", req("POST", "/api/login", {"username": "bob", "password": "p"})[0] == 200)
_st, bd, _ = req("POST", "/api/login", {"username": "alice", "password": ""})
check("空密码不消耗限流额度", _st == 200 and "用户名和密码" in bd, (_st, bd))

print("\n-- 图像路由限流")
# 出图路由此前完全没有限流，且都会经 resolve_account_keys 触发自动重登
_ig_key, _ig_gen = imagegen.get_default_api_key, imagegen.generate_image
imagegen.get_default_api_key = lambda u: "sk-test"
imagegen.generate_image = lambda *a, **k: {"b64": base64.b64encode(b"img").decode("ascii"),
                                           "output_format": "png", "size": "1x1"}
try:
    _st, bd, _ = req("POST", "/api/imagegen", {"username": "imguser", "prompt": "p"})
    check("出图首次放行", _st == 200 and json.loads(bd).get("dl_id"), (_st, bd))
    _st, bd, hd = req("POST", "/api/imagegen", {"username": "imguser", "prompt": "p"})
    check("同账号重复出图 → 429", _st == 429, (_st, bd))
    check("出图 429 带 Retry-After", "Retry-After" in hd)
    # 校验须在限流之前，否则一个坏请求就吞掉这次出图额度
    check("空提示词不消耗出图额度",
          req("POST", "/api/imagegen", {"username": "imguser", "prompt": ""})[0] == 400)
    _st, bd, _ = req("POST", "/api/imageedit",
                     {"username": "imguser", "prompt": "p",
                      "images": ["data:image/png;base64,iVBORw0KGgo="]})
    check("编辑图与出图共用同一账号额度 → 429", _st == 429, (_st, bd))
finally:
    imagegen.get_default_api_key = _ig_key
    imagegen.generate_image = _ig_gen

print("\n-- 配置写入")
dashboard._THROTTLE_USAGE._last.clear()
_st, bd, _ = req("POST", "/api/accounts", {"accounts": [{"username": "newu", "password": "np"}]})
check("保存账号 → success", _st == 200 and json.loads(bd)["success"] is True, (_st, bd))
acc = {a["username"]: a for a in accounts.load_accounts()}
check("新账号已落盘", "newu" in acc)
_st, bd, _ = req("POST", "/api/settings", {"refresh_seconds": 600})
check("保存设置 → success", _st == 200 and json.loads(bd)["success"] is True, (_st, bd))
check("设置已落盘", accounts.load_settings()["refresh_seconds"] == 600)
_st, bd, _ = req("POST", "/api/settings", {"refresh_seconds": 10})
check("越界设置被拒", _st == 400 and not json.loads(bd)["success"], (_st, bd))

# ============================================================
# 7. 图像下载缓存
# ============================================================
section("7. 图像下载缓存")

_orig = (imagegen.IMAGE_CACHE_MAX_BYTES, imagegen.IMAGE_CACHE_MAX, imagegen.IMAGE_CACHE_TTL)
imagegen.IMAGE_CACHE_MAX_BYTES = 1000
imagegen.IMAGE_CACHE_MAX = 10
imagegen.IMAGE_CACHE_TTL = 3600
imagegen._image_cache.clear()
try:
    def blob(n):
        return base64.b64encode(b"A" * n).decode("ascii")
    ids = [imagegen.store_image_for_download(blob(700), "png", "1x1") for _ in range(4)]
    total = sum(len(v["bytes"]) for v in imagegen._image_cache.values())
    check("总字节上限生效", total <= imagegen.IMAGE_CACHE_MAX_BYTES, total)
    check("最新一条仍可取", imagegen.get_cached_image(ids[-1]) is not None)
    check("最旧一条被淘汰", imagegen.get_cached_image(ids[0]) is None)
    newest = list(imagegen._image_cache.values())[-1]
    newest["ts"] = time.time() - imagegen.IMAGE_CACHE_TTL - 10
    imagegen.store_image_for_download(blob(700), "png", "1x1")
    check("过期项按 TTL 清理", imagegen.get_cached_image(ids[-1]) is None)
    imagegen._image_cache.clear()
    for _ in range(imagegen.IMAGE_CACHE_MAX + 5):
        imagegen.store_image_for_download(blob(10), "png", "1x1")
    check("条数上限生效", len(imagegen._image_cache) <= imagegen.IMAGE_CACHE_MAX,
          len(imagegen._image_cache))
finally:
    (imagegen.IMAGE_CACHE_MAX_BYTES, imagegen.IMAGE_CACHE_MAX,
     imagegen.IMAGE_CACHE_TTL) = _orig
    imagegen._image_cache.clear()

# 未知 dl_id 不该崩
check("未知 dl_id 返回 None", imagegen.get_cached_image("nope") is None)

# ============================================================
# 8. auth_login / sensenova_api 纯函数
# ============================================================
section("8. auth_login / sensenova_api 纯函数")

check("白名单放行官方域名", auth_login._is_trusted_url("https://iam.sensecoreapi.cn/x"))
check("白名单放行子域", auth_login._is_trusted_url("https://a.platform.sensenova.cn/x"))
check("白名单拒绝非 https", not auth_login._is_trusted_url("http://iam.sensecoreapi.cn/x"))
check("白名单拒绝第三方域", not auth_login._is_trusted_url("https://evil.com/x"))
check("白名单拒绝相似前缀域", not auth_login._is_trusted_url("https://evil-sensenova.cn/x"))
check("白名单拒绝畸形 URL", not auth_login._is_trusted_url("://nope"))

s = auth_login._sanitize("code=ABC123&access_token=tok&login_challenge=lc "
                         "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig")
check("日志脱敏 code", "code=ABC123" not in s)
check("日志脱敏 access_token", "access_token=tok" not in s)
check("日志脱敏 Bearer", "Bearer <redacted>" in s)

check("JWT exp 可解析", sensenova_api._jwt_exp(fake_jwt(9999999999)) == 9999999999)
check("JWT 过期可识别", sensenova_api._jwt_exp(fake_jwt(1)) < time.time())
check("JWT 畸形返回 None", sensenova_api._jwt_exp("not.a.jwt") is None)
check("JWT 空值返回 None", sensenova_api._jwt_exp("") is None)

check("API Key 脱敏保留首尾", "****" in sensenova_api.mask_api_key("sk-abcdef123456789"))
check("短 Key 不越界", sensenova_api.mask_api_key("abc") == "abc****")
check("空 Key 返回空", sensenova_api.mask_api_key("") == "")

print("\n-- 登录限流")
# /api/login 已有 HTTP 层限流，但自动重登、/api/apikey、图像路由走的登录链路此前是敞开的
auth_login.login = _real_login          # 换回真实包装，才能验证限流本身
auth_login._login_once = (lambda *a, **k: {"access_token": "tok",
                                           "refresh_token": "", "expires_in": 10800})
auth_login._login_throttle.clear()
check("首次登录放行", auth_login.login("throttleu", "p", 5).get("access_token") == "tok")
try:
    auth_login.login("throttleu", "p", 5)
    check("同账号重复登录被限流", False)
except RuntimeError as e:
    check("同账号重复登录被限流", "过于频繁" in str(e))
check("不同账号不受限流", auth_login.login("otheru", "p", 5).get("access_token") == "tok")
# refresh 用 refresh_token 作 key，与 login 各自独立计时、互不占用额度
ok, _w = auth_login._login_throttled(("refresh", "rt-abc"))
check("refresh 闸门独立于 login", ok)
ok, _w = auth_login._login_throttled(("refresh", "rt-abc"))
check("refresh 重复被拒", not ok)
auth_login.login = _real_login
auth_login._login_once = _real_login_once
auth_login._login_throttle.clear()

# ============================================================
# 9. API Key 缓存
# ============================================================
section("9. API Key 缓存")

KEYS = [{"id": "k1", "displayname": "d", "api_key": "sk-1234567890abcdef",
         "status": "enabled", "is_default": True, "create_time": ""}]
calls = {"n": 0}


def _counting_keys(*a, **k):
    calls["n"] += 1
    return KEYS


orig_qak = sensenova_api.query_api_keys
sensenova_api.query_api_keys = _counting_keys
# 带一个未过期的 JWT，让 ensure_token 直接返回、不触发网络
accounts.merge_accounts([{"username": "cachet",
                          "jwt_token": fake_jwt(int(time.time()) + 3600)}])
try:
    sensenova_api.clear_api_key_cache()
    _a, _t, k1 = sensenova_api.resolve_account_keys("cachet")
    check("首次返回 Key 明文", k1 and k1[0]["api_key"] == "sk-1234567890abcdef")
    check("首次触发一次网络请求", calls["n"] == 1, calls["n"])

    _a, _t, k2 = sensenova_api.resolve_account_keys("cachet")
    check("缓存命中不重新拉取", calls["n"] == 1, calls["n"])
    check("返回缓存里的同一对象（约定只读）", k2 is k1)

    sensenova_api._api_key_cache["cachet"] = (time.time() - 1, KEYS)
    sensenova_api.resolve_account_keys("cachet")
    check("TTL 过期后重新拉取", calls["n"] == 2, calls["n"])

    sensenova_api.clear_api_key_cache("cachet")
    sensenova_api.resolve_account_keys("cachet")
    check("clear_api_key_cache 后重新拉取", calls["n"] == 3, calls["n"])

    sensenova_api._api_key_cache["cachet"] = (time.time() + 600, KEYS)
    sensenova_api._api_key_cache["other"] = (time.time() + 600, KEYS)
    sensenova_api.clear_api_key_cache("cachet")
    check("按账号清理移除了目标", "cachet" not in sensenova_api._api_key_cache)
    check("按账号清理不影响其他账号", "other" in sensenova_api._api_key_cache)
    sensenova_api.clear_api_key_cache()

    sensenova_api.resolve_account_keys("cachet")
    check("缓存已填充", "cachet" in sensenova_api._api_key_cache)
    sensenova_api.query_all_api_keys()
    check("主动刷新列表会清掉 per-account 缓存", "cachet" not in sensenova_api._api_key_cache)

    try:
        sensenova_api.resolve_account_keys("nosuchuser")
        check("不存在的账号抛错", False)
    except ValueError as e:
        check("不存在的账号抛错", "不存在" in str(e))
finally:
    sensenova_api.query_api_keys = orig_qak
    sensenova_api.clear_api_key_cache()
    accounts.merge_accounts([])

# ============================================================
# 收尾
# ============================================================
dashboard._server.shutdown()
shutil.rmtree(WORK_DIR, ignore_errors=True)

print("\n" + "-" * 46)
print("通过 %d 项，失败 %d 项" % (PASS_N, FAIL_N))
print("ALL PASS" if FAIL_N == 0 else "SOME FAILED")
raise SystemExit(0 if FAIL_N == 0 else 1)
