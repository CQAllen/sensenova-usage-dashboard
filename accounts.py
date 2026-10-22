#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 账号配置读写层

在 credstore（加密存储）之上提供账号列表与界面设置的高层读写，
含并发锁与配置迁移/解密类告警的暂存。sanitize_error 也放在这里供各模块复用。
"""

from __future__ import annotations

import re
import threading
from typing import Any

import credstore

# 配置目录（存放加密后的 accounts.json 与密钥），由 credstore 统一决定：
# 打包态用用户级目录；源码态默认脚本目录，命中云同步盘时改存用户级目录。
BASE_DIR = credstore.resolve_config_dir()

# 可持久化到配置文档的界面设置（与账号同文件，同样加密存储）
AUTO_REFRESH_SECONDS = 300                  # 默认自动刷新间隔（秒）
DEFAULT_SETTINGS = {"refresh_seconds": AUTO_REFRESH_SECONDS,
                    "close_behavior": "exit",       # exit（默认）| minimize（最小化到托盘）
                    "auto_start": False}
REFRESH_MIN, REFRESH_MAX = 60, 3600         # 自动刷新间隔允许范围

_accounts_lock = threading.Lock()
# 配置迁移/解密类告警，随下一次 /api/usage 一并回传给界面提示一次
_config_warnings: list[str] = []


def _read_doc() -> tuple[dict, str | None]:
    """读取配置文档，返回 (doc, warning)。doc 始终为 dict。"""
    obj, warning = credstore.read_document(BASE_DIR)
    if warning and warning not in _config_warnings:
        _config_warnings.append(warning)
        print(f"配置提示: {warning}")
    if not isinstance(obj, dict):
        obj = {}
    obj.setdefault("accounts", [])
    return obj, warning


def _mutate_doc(fn: Any) -> Any:
    """在锁内「读取 → 修改 → 写回」配置文档，返回 fn(doc) 的结果，规避并发读改写竞态。"""
    with _accounts_lock:
        doc, _ = _read_doc()
        result = fn(doc)
        try:
            credstore.write_document(BASE_DIR, doc)
        except Exception as e:
            # 必须向上抛：界面按 success 提示「已保存」，吞掉等于写盘失败却报成功
            print(f"写入配置失败: {e}")
            raise
        return result


def _mutate_accounts(fn: Any) -> Any:
    """在锁内修改账号列表，返回 fn(accounts) 的结果。"""
    def _apply(doc):
        accounts = doc.get("accounts", [])
        result = fn(accounts)
        doc["accounts"] = accounts
        return result

    return _mutate_doc(_apply)


def load_accounts() -> list[dict]:
    """返回当前配置中的账号列表（不解密无关字段，直接返回 dict 引用）。"""
    with _accounts_lock:
        doc, _ = _read_doc()
    return doc.get("accounts", [])


def load_settings() -> dict:
    """返回合并默认值后的设置（缺失/越界字段回退到合法值）。"""
    with _accounts_lock:
        doc, _ = _read_doc()
    settings = dict(DEFAULT_SETTINGS)
    stored = doc.get("settings")
    if isinstance(stored, dict):
        settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    try:
        secs = int(settings["refresh_seconds"])
    except (TypeError, ValueError):
        secs = AUTO_REFRESH_SECONDS
    settings["refresh_seconds"] = min(max(secs, REFRESH_MIN), REFRESH_MAX)
    return settings


def save_settings(incoming: dict) -> dict:
    """校验并持久化设置，返回落盘后的设置。

    允许只提交部分字段：未提交的字段沿用已保存值，避免「只改刷新间隔」
    把关闭行为、开机自启一并重置成默认值。
    """
    if not isinstance(incoming, dict):
        raise ValueError("settings 必须是对象")
    if "refresh_seconds" in incoming:
        try:
            secs = int(incoming.get("refresh_seconds") or 0)
        except (TypeError, ValueError):
            raise ValueError("refresh_seconds 必须是整数")
        if not (REFRESH_MIN <= secs <= REFRESH_MAX):
            raise ValueError(f"refresh_seconds 需在 {REFRESH_MIN}-{REFRESH_MAX} 秒之间")
    else:
        secs = None

    def _apply(doc):
        stored = doc.get("settings")
        if not isinstance(stored, dict):
            stored = {}
        settings = dict(DEFAULT_SETTINGS)
        # 只继承已知字段，忽略历史遗留或未知键
        settings.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})

        if secs is not None:
            settings["refresh_seconds"] = secs
        else:
            try:
                settings["refresh_seconds"] = int(settings["refresh_seconds"])
            except (TypeError, ValueError):
                settings["refresh_seconds"] = AUTO_REFRESH_SECONDS
            settings["refresh_seconds"] = min(max(settings["refresh_seconds"],
                                                  REFRESH_MIN), REFRESH_MAX)

        if "close_behavior" in incoming:
            cb = incoming.get("close_behavior")
            if cb in ("minimize", "exit"):
                settings["close_behavior"] = cb
        if "auto_start" in incoming:
            settings["auto_start"] = bool(incoming.get("auto_start"))

        doc["settings"] = settings
        return settings

    return _mutate_doc(_apply)


def update_account(username: str, **fields: Any) -> None:
    """按用户名更新字段（如登录后回写 jwt_token / password 等）。"""
    def _apply(accounts):
        for a in accounts:
            if a.get("username") == username:
                a.update(fields)
                return
        new = {"username": username}
        new.update(fields)
        accounts.append(new)

    _mutate_accounts(_apply)


def merge_accounts(incoming: list) -> None:
    """合并前端提交的账号列表：
    - 密码留空 → 沿用已保存值（password_clear=True 才清除）
    - jwt_token 留空 → 沿用已保存值（JWT 只写不读，绝不回传浏览器）
    - 提交中不存在的用户名 → 删除
    """
    if not isinstance(incoming, list):
        raise ValueError("accounts 必须是数组")

    def _apply(accounts):
        existing = {}
        for a in accounts:
            if a.get("username"):
                existing[a["username"]] = a
        result = []
        seen = set()
        for item in incoming:
            if not isinstance(item, dict):
                continue
            uname = (item.get("username") or "").strip()
            if not uname or uname in seen:
                continue
            seen.add(uname)
            old = existing.get(uname) or {}
            if item.get("password_clear"):
                pwd = ""
            else:
                pwd = item.get("password") or old.get("password", "")
            new = {"username": uname, "password": pwd,
                   "jwt_token": item.get("jwt_token") or old.get("jwt_token", "")}
            for k in ("token_acquired_at", "token_expires_in", "refresh_token"):
                if k in old:
                    new[k] = old[k]
            result.append(new)
        accounts[:] = result

    _mutate_accounts(_apply)


def pop_config_warnings() -> list[str]:
    """返回已暂存的配置告警列表并清空（供 /api/usage 路由随结果回传一次）。"""
    with _accounts_lock:
        warnings = list(_config_warnings)
        _config_warnings.clear()
    return warnings


def sanitize_error(err: object, limit: int = 150) -> str:
    """脱敏错误信息：去除绝对路径、疑似 JWT 与超长串，避免泄漏本机信息。"""
    s = str(err)
    s = re.sub(r"[A-Za-z]:\\[^\s'\"]+", "<path>", s)
    s = re.sub(r"/(?:home|Users)/[^/\s'\"]+", "/<user>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", "<token>", s)
    s = re.sub(r"[A-Za-z0-9_\-]{40,}", "<redacted>", s)
    return s[:limit]
