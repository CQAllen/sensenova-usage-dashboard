#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - API 查询与数据解析层

负责：积分池用量、API Key 列表查询，JWT 自动刷新（ensure_token），
以及遍历多账号的 _collect_accounts。依赖 accounts（配置层）与 auth_login（登录）。
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import requests

import accounts
import auth_login

_log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30                        # API 请求超时（秒）
API_BASE = "https://platform.sensenova.cn"
POOL_USAGE_ENDPOINT = "/lite/console/v1/tokenplan/pool-usage"
API_KEYS_ENDPOINT = "/lite/console/v1/metered/api-keys"
API_KEYS_PAGE_SIZE = 100                    # 单页拉取量，避免翻页


def _jwt_exp(token: str) -> int | None:
    """从 JWT payload 解码 exp 声明（Unix 时间戳），失败返回 None。"""
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None


def _auth_headers(token: str) -> dict:
    return {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "Referer": f"{API_BASE}/console",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }


def _session():
    """返回禁用环境变量代理的 Session：请求头带 Bearer JWT，不能被中间代理看到。"""
    s = requests.Session()
    s.trust_env = False
    return s


def query_pool_usage(token: str, timeout: int = 30) -> dict:
    with _session() as s:
        resp = s.get(API_BASE + POOL_USAGE_ENDPOINT,
                     headers=_auth_headers(token), timeout=timeout)
        resp.raise_for_status()
        return resp.json()


def query_api_keys(token: str, timeout: int = 30) -> list:
    """拉取某账号的 Token Plan API Key 列表（含明文，仅在后端内部流转）。"""
    params = {"key_type": "API_KEY_TYPE_TOKEN_PLAN", "page_size": API_KEYS_PAGE_SIZE}
    with _session() as s:
        resp = s.get(API_BASE + API_KEYS_ENDPOINT, headers=_auth_headers(token),
                     params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    keys = data.get("api_keys") if isinstance(data, dict) else None
    return keys if isinstance(keys, list) else []


def mask_api_key(key: str) -> str:
    """脱敏：保留前缀与末 4 位，中间用星号替代。"""
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) <= 12:
        return key[:3] + "****"
    return key[:7] + "****" + key[-4:]


def _iso_to_local(iso: Any) -> str:
    """接口返回的是 UTC ISO 时间，转成本地「YYYY-MM-DD HH:MM」；解析失败原样返回。"""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(iso or "")


def parse_api_keys(raw_keys: list) -> list[dict]:
    """整理成前端可用结构。明文一律丢弃，只保留脱敏值，绝不进入 HTTP 响应。"""
    out = []
    for k in raw_keys:
        if not isinstance(k, dict):
            continue
        out.append({
            "id": str(k.get("id") or ""),
            "name": str(k.get("displayname") or ""),
            "masked": mask_api_key(k.get("api_key") or ""),
            "status": str(k.get("status") or ""),
            "is_default": bool(k.get("is_default")),
            "create_time": _iso_to_local(k.get("create_time")),
        })
    return out


# API Key 缓存：resolve_account_keys 的调用方（图像生成、显示明文）每次都重新拉取 Key 列表，
# 而列表在几分钟内不会变。按用户名缓存原始响应，TTL 到期或 clear_api_key_cache() 时失效。
# JWT 不缓存——ensure_token 每次仍会校验 exp，缓存只省掉那次网络往返。
API_KEY_CACHE_TTL = 300                   # Key 列表缓存有效期（秒）
_api_key_cache: dict[str, tuple[float, list]] = {}                       # username -> (过期时间戳, keys)
_api_key_cache_lock = threading.Lock()


def clear_api_key_cache(username: str | None = None) -> None:
    """清 API Key 缓存；username 为 None 时清空全部。"""
    with _api_key_cache_lock:
        if username is None:
            _api_key_cache.clear()
        else:
            _api_key_cache.pop((username or "").strip(), None)


def _cached_api_keys(username: str, token: str, timeout: int) -> list:
    """带 TTL 的 query_api_keys：命中直接返回，否则拉取后写入缓存。

    返回的是缓存里的列表对象，调用方只读；需要修改请自行拷贝。
    """
    key = (username or "").strip()
    now = time.time()
    with _api_key_cache_lock:
        hit = _api_key_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    keys = query_api_keys(token, timeout)
    with _api_key_cache_lock:
        _api_key_cache[key] = (now + API_KEY_CACHE_TTL, keys)
    return keys


def resolve_account_keys(username: str) -> tuple[dict, str, list]:
    """定位账号 → 确保 JWT → 取 API Key 列表（带 TTL 缓存），返回 (account, token, keys)。
    JWT 刷新结果写回加密配置。供「显示/复制明文」「图像生成」等按需取 Key 的流程复用。"""
    account = None
    for a in accounts.load_accounts():
        if (a.get("username") or "").strip() == username:
            account = a
            break
    if account is None:
        raise ValueError("账号不存在")
    token, updates, err = ensure_token(account, REQUEST_TIMEOUT)
    if updates:
        try:
            accounts.update_account(username, **updates)
        except Exception as e:
            _log.warning("写回账号 %s 的 token 刷新结果失败: %s", username, e)
    if err or not token:
        raise ValueError(err or "无法获取登录态")
    keys = _cached_api_keys(username, token, REQUEST_TIMEOUT)
    return account, token, keys


def get_api_key_plain(username: str, key_id: str) -> str:
    """按需取单个 Key 的明文，仅供界面点「显示 / 复制」时一次性返回。"""
    if not username or not key_id:
        raise ValueError("参数不完整")
    _, _, keys = resolve_account_keys(username)
    for k in keys:
        if isinstance(k, dict) and str(k.get("id") or "") == key_id:
            return str(k.get("api_key") or "")
    raise ValueError("未找到该 API Key")


def fmt_num(n: float) -> str:
    """数值转带千分位的字符串：整数去小数点，非整数保留一位。"""
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def ts_to_str(ts_str: Any) -> str:
    """Unix 时间戳字符串转「MM-DD HH:MM」；无效原样返回。"""
    if not ts_str or ts_str == "0":
        return ""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts_str)


def ts_to_date(ts_str: Any) -> str:
    """Unix 时间戳字符串转「YYYY-MM-DD」；无效原样返回。"""
    if not ts_str or ts_str == "0":
        return ""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(ts_str)


def _to_ts(ts_str: Any) -> int:
    """将时间戳字符串转为 int，无效返回 0。"""
    if not ts_str or ts_str == "0":
        return 0
    try:
        return int(ts_str)
    except (ValueError, TypeError):
        return 0


def parse_pool_data(raw: dict) -> list[dict]:
    results = []
    pools = raw.get("pools") or []
    if isinstance(pools, dict):
        pools = list(pools.values())

    for pool in pools:
        if not isinstance(pool, dict):
            continue

        pool_name = pool.get("name", "未知积分池")
        model_ids = pool.get("model_ids", [])
        model_display = ", ".join(model_ids[:3])
        if len(model_ids) > 3:
            model_display += f" 等{len(model_ids)}个"

        w5h = pool.get("window_5h") or {}
        w7d = pool.get("window_7d") or {}

        try:
            limit_5h = float(w5h.get("limit", "0"))
            used_5h = float(w5h.get("used", "0"))
            remain_5h = float(w5h.get("remaining", "0"))
        except (ValueError, TypeError):
            limit_5h = used_5h = remain_5h = 0.0

        pct_5h = round(used_5h / limit_5h * 100, 1) if limit_5h > 0 else 0.0

        try:
            limit_7d = float(w7d.get("limit", "0"))
            used_7d = float(w7d.get("used", "0"))
            remain_7d = float(w7d.get("remaining", "0"))
        except (ValueError, TypeError):
            limit_7d = used_7d = remain_7d = 0.0

        pct_7d = round(used_7d / limit_7d * 100, 1) if limit_7d > 0 else 0.0

        try:
            grant_f = float(pool.get("grant_balance", "0"))
        except (ValueError, TypeError):
            grant_f = 0.0

        pool_type = pool.get("pool_type", "")

        results.append({
            "pool_name": pool_name,
            "pool_type": pool_type,
            "model_display": model_display,
            "limit_5h": fmt_num(limit_5h),
            "used_5h": fmt_num(used_5h),
            "remain_5h": fmt_num(remain_5h),
            "limit_5h_raw": limit_5h,
            "used_5h_raw": used_5h,
            "remain_5h_raw": remain_5h,
            "usage_pct_5h": pct_5h,
            "reset_at": ts_to_str(w5h.get("reset_at", "")),
            "reset_at_ts": _to_ts(w5h.get("reset_at", "")),
            "reset_at_7d": ts_to_str(w7d.get("reset_at", "")),
            "reset_at_7d_ts": _to_ts(w7d.get("reset_at", "")),
            "limit_7d": fmt_num(limit_7d),
            "used_7d": fmt_num(used_7d),
            "remain_7d": fmt_num(remain_7d),
            "limit_7d_raw": limit_7d,
            "used_7d_raw": used_7d,
            "remain_7d_raw": remain_7d,
            "usage_pct_7d": pct_7d,
            "grant_balance": fmt_num(grant_f),
            "nearest_expiry": ts_to_date(pool.get("nearest_grant_expiry", "0")),
        })

    return results


def ensure_token(account: dict, timeout: int) -> tuple[str | None, dict | None, str | None]:
    """确保 account 持有可用 JWT：无 token 或即将过期时刷新。
    优先用 refresh_token 刷新（更轻、不易触发风控），失败降级为账号密码登录。
    返回 (token, updates, error)：
      - updates 为需要写回配置的字段 dict（无需更新时为 None）
      - error 非 None 时表示登录失败原因（已脱敏，可直接透传到界面）"""
    token = (account.get("jwt_token") or "").strip()

    # ---- 1) 直接解码 JWT exp 判断是否过期/即将过期 ----
    need_refresh = not token
    jwt_exp = _jwt_exp(token)
    if jwt_exp:
        # 提前 5 分钟刷新，避免边界 race
        if jwt_exp - time.time() < 300:
            need_refresh = True
    else:
        # 无法解码 exp（格式异常），保守地尝试刷新
        if token:
            need_refresh = True

    if not need_refresh:
        return token, None, None

    # ---- 2) 优先 refresh_token 刷新 ----
    rt = (account.get("refresh_token") or "").strip()
    if rt:
        try:
            res = auth_login.refresh(rt, timeout)
            new_token = res.get("access_token", "")
            if new_token:
                account["jwt_token"] = new_token  # 让本次查询立即可用
                return new_token, _token_updates(res, new_token,
                                                 refresh_token_fallback=rt), None
        except Exception:
            pass  # refresh 失败（平台不支持 / token 失效），降级密码登录

    # ---- 3) 用账号密码自动登录 ----
    uname = (account.get("username") or "").strip()
    pwd = (account.get("password") or "").strip()
    if not uname or not pwd:
        return token, None, "JWT 已过期且无账号密码，无法自动登录"

    try:
        res = auth_login.login(uname, pwd, timeout)
    except Exception as e:
        return token, None, f"自动登录失败: {accounts.sanitize_error(e)}"

    new_token = res.get("access_token", "")
    if not new_token:
        return token, None, "自动登录未返回 token"
    account["jwt_token"] = new_token  # 让本次查询立即可用
    return new_token, _token_updates(res, new_token), None


def _token_updates(res: dict, token: str, refresh_token_fallback: str = "") -> dict:
    """把登录 / 刷新接口响应转成写回配置的 token 字段，供各登录分支复用。

    refresh_token 未随响应轮换时（刷新接口通常如此）沿用旧值。
    """
    return {
        "jwt_token": token,
        "refresh_token": res.get("refresh_token", "") or refresh_token_fallback,
        "token_acquired_at": time.time(),
        "token_expires_in": res.get("expires_in") or auth_login.DEFAULT_EXPIRES_IN,
    }


def parse_plan(raw: dict) -> str:
    """取账号当前套餐的显示名（如 "Free Plan"）；字段缺失时返回空串。"""
    plan = raw.get("plan")
    if not isinstance(plan, dict):
        return ""
    return str(plan.get("name") or plan.get("id") or "").strip()


def _process_one(account: dict, fetch: Any, empty: dict | None, timeout: int) -> tuple[str, dict, dict | None]:
    """处理单个账号：取登录态 → fetch → 401 重登一次。返回 (name, entry, updates_or_None)。
    每个账号只动自己的 dict，互不交叉，可并发执行。"""
    name = (account.get("username") or "未知").strip()
    token, updates, login_err = ensure_token(account, timeout)

    if login_err:
        entry = {"username": name, "error": login_err}
        entry.update(empty or {})
        return name, entry, (updates or None)

    if not token:
        entry = {"username": name, "error": "未配置 JWT 且无账号密码，无法自动登录"}
        entry.update(empty or {})
        return name, entry, None

    try:
        entry = {"username": name, "error": None}
        entry.update(fetch(token, timeout))
        return name, entry, (updates or None)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        # 401 多半是 token 失效，尝试用账号密码重新登录一次
        if status == 401 and account.get("username") and account.get("password"):
            try:
                res = auth_login.login(account["username"], account["password"], timeout)
                token = res.get("access_token", "")
                upd = _token_updates(res, token)
                entry = {"username": name, "error": None}
                entry.update(fetch(token, timeout))
                return name, entry, upd
            except Exception as e2:
                entry = {"username": name,
                         "error": f"JWT 过期后自动重登失败: {accounts.sanitize_error(e2, 120)}"}
                entry.update(empty or {})
                return name, entry, (updates or None)
        if status == 401:
            err = "JWT 已过期，请用账号密码重新登录"
        elif status == 403:
            err = "权限不足"
        else:
            err = f"HTTP {status}"
        entry = {"username": name, "error": err}
        entry.update(empty or {})
        return name, entry, (updates or None)
    except Exception as e:
        entry = {"username": name, "error": accounts.sanitize_error(e, 100)}
        entry.update(empty or {})
        return name, entry, (updates or None)


def _collect_accounts(fetch: Any, empty: dict | None = None) -> list[dict]:
    """遍历所有账号并发查询：取登录态 → fetch → 401 重登。
    每账号一线程（上限 8），结果按账号顺序返回；token 刷新结果写回加密配置。"""
    timeout = REQUEST_TIMEOUT
    acct_list = list(accounts.load_accounts())
    if not acct_list:
        return []

    workers = min(8, len(acct_list))
    results = []
    pending_updates = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_process_one, a, fetch, empty, timeout) for a in acct_list]
        for fut in futs:
            name, entry, updates = fut.result()
            results.append(entry)
            if updates:
                pending_updates[name] = updates

    # 把自动登录刷新出的 token 写回加密配置（写盘失败不影响本次查询结果）
    for uname, updates in pending_updates.items():
        try:
            accounts.update_account(uname, **updates)
        except Exception as e:
            _log.warning("写回账号 %s 的自动登录结果失败: %s", uname, e)

    return results


def query_all_accounts() -> dict:
    """各账号的积分池用量与当前套餐。"""
    def _fetch(token, timeout):
        raw = query_pool_usage(token, timeout)
        return {"items": parse_pool_data(raw), "plan": parse_plan(raw)}

    result = {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "accounts": _collect_accounts(_fetch, empty={"items": []})}
    # 记录用量快照——失败不影响查询结果展示
    try:
        import history
        history.record_snapshots(accounts.BASE_DIR, result)
    except Exception:
        pass
    return result


def query_all_api_keys() -> dict:
    """各账号的 API Key 列表（只含脱敏值）。"""
    # 用户主动刷新的这份列表应当是最新的，顺带清掉 per-account 缓存，
    # 让随后的图像生成 / 显示明文也走新的数据，而不是用到旧缓存
    clear_api_key_cache()

    def _fetch(token, timeout):
        return {"keys": parse_api_keys(query_api_keys(token, timeout))}

    return {"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "accounts": _collect_accounts(_fetch, empty={"keys": []})}
