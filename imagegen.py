#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 图像生成 / 编辑 / 下载层

基于商汤日日新 U1.5 模型（token.sensenova.cn），用账号的 Token Plan API Key
作为 Bearer token 调用文生图与图片编辑。生成结果以原始字节缓存，供
/api/imagedl 与 pywebview 桥接（ImageApi）原生保存。
"""

from __future__ import annotations

import base64
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests
import webview

import accounts
import sensenova_api

IMAGE_API_BASE = "https://token.sensenova.cn"
IMAGE_GEN_ENDPOINT = "/v1/images/generations"
IMAGE_EDIT_ENDPOINT = "/v1/images/edits"
IMAGE_REQUEST_TIMEOUT = 180                 # 4K / Lite 出图较慢，留足余量

# 生成图下载缓存：dl_id -> {bytes, fmt, size, ts}，供 /api/imagedl 原生下载
_image_cache: dict[str, dict] = {}
_image_cache_lock = threading.Lock()
IMAGE_CACHE_TTL = 3600                      # 缓存有效期（秒）
IMAGE_CACHE_MAX = 50                         # 缓存条数上限，超出清最旧
# 总字节上限：4K PNG 单张可达 30MB+，仅有条数上限时长期挂机可能常驻上 GB 内存
IMAGE_CACHE_MAX_BYTES = 256 * 1024 * 1024
MAX_IMAGE_BYTES = 64 * 1024 * 1024       # 回退下载的单张体积上限，避免外部响应吃满内存

# 图像 URL 回退下载的可信域名白名单（防 SSRF：API 返回的 url 只允许落在商汤域）
_IMAGE_TRUSTED_HOSTS = ("sensenova.dev", "sensenova.cn", "sensecoreapi.cn")

# 允许的图片格式白名单：output_format 来自外部响应，需过滤后再用于响应头 / 文件名 / MIME
ALLOWED_IMAGE_FMTS = ("png", "jpeg", "jpg", "webp")


def _sanitize_image_fmt(fmt: str) -> str:
    f = (fmt or "").strip().lower()
    return f if f in ALLOWED_IMAGE_FMTS else "png"


def _valid_image_size(size: str) -> bool:
    """校验尺寸：auto 或 WIDTHxHEIGHT（宽高为 32 倍数、512–4096、比例 ≤ 3:1）。"""
    if not size or size == "auto":
        return True
    m = re.match(r"^(\d+)x(\d+)$", size)
    if not m:
        return False
    w, h = int(m.group(1)), int(m.group(2))
    if w % 32 != 0 or h % 32 != 0:
        return False
    if not (512 <= w <= 4096 and 512 <= h <= 4096):
        return False
    ratio = max(w, h) / min(w, h)
    return ratio <= 3.0


def _is_trusted_image_url(url: str) -> bool:
    """仅允许 https 且落在商汤可信域名的图像下载地址，防 SSRF。"""
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _IMAGE_TRUSTED_HOSTS)


def _session():
    """返回禁用环境变量代理的 Session：请求头带 Bearer API Key，不能被中间代理看到。"""
    s = requests.Session()
    s.trust_env = False
    return s


def _post_image_api(api_key: str, endpoint: str, payload: dict) -> dict:
    """统一调用图像接口：POST + 状态码检查 + 错误解析。成功返回 JSON dict。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with _session() as s:
        resp = s.post(IMAGE_API_BASE + endpoint, headers=headers,
                      json=payload, timeout=IMAGE_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            # 鉴权类错误结构可能没有 type 字段，以 error.message 为准
            msg = resp.text
            try:
                ej = (resp.json().get("error") or {})
                if ej.get("message"):
                    msg = ej["message"]
            except Exception:
                pass
            raise ValueError(f"图像生成失败 (HTTP {resp.status_code}): {msg[:300]}")
        return resp.json()


def _extract_image_data(data: dict, size: str) -> dict:
    """从响应里取 base64 图像数据；兼容个别情况下返回 url（后端代为下载转 base64）。"""
    items = data.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ValueError("图像生成返回为空")
    b64 = items[0].get("b64_json") or ""
    if not b64 and items[0].get("url"):
        img_url = items[0]["url"]
        if not _is_trusted_image_url(img_url):
            raise ValueError("图像生成返回的下载地址不在可信域名，已拒绝下载")
        # 限读：返回体来自外部响应，流式读取并设体积上限，避免超大响应吃满内存
        with _session() as s:
            r2 = s.get(img_url, timeout=IMAGE_REQUEST_TIMEOUT, stream=True)
            r2.raise_for_status()
            chunks, total = [], 0
            for chunk in r2.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ValueError(f"图像下载体积超过 {MAX_IMAGE_BYTES // (1024 * 1024)}MB 上限，已中止")
                chunks.append(chunk)
        b64 = base64.b64encode(b"".join(chunks)).decode("ascii")
    if not b64:
        raise ValueError("图像生成返回无图像数据")
    # output_format 来自外部响应，白名单过滤以防头注入 / 异常 MIME
    fmt = _sanitize_image_fmt(data.get("output_format") or "")
    return {"b64": b64, "output_format": fmt, "size": data.get("size") or size}


def store_image_for_download(b64: str, fmt: str, size: str | None) -> str:
    """把生成结果缓存为可下载的原始字节，返回 dl_id 供前端跳转 /api/imagedl 下载。
    用字节缓存而非 base64，避免下载时重复解码；同时顺带清理过期项。"""
    dl_id = secrets.token_urlsafe(16)
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raw = b""
    now = time.time()
    with _image_cache_lock:
        _image_cache[dl_id] = {"bytes": raw, "fmt": fmt or "png", "size": size, "ts": now}
        cutoff = now - IMAGE_CACHE_TTL
        for k in list(_image_cache.keys()):
            if _image_cache[k]["ts"] < cutoff:
                del _image_cache[k]
        # 条数上限：超出时按时间戳删最旧
        if len(_image_cache) > IMAGE_CACHE_MAX:
            overflow = len(_image_cache) - IMAGE_CACHE_MAX
            for k, _v in sorted(_image_cache.items(), key=lambda kv: kv[1]["ts"])[:overflow]:
                del _image_cache[k]
        # 总字节上限：同样从最旧删起；至少保留刚写入的这一条
        total = sum(len(v["bytes"]) for v in _image_cache.values())
        while len(_image_cache) > 1 and total > IMAGE_CACHE_MAX_BYTES:
            oldest = min(_image_cache, key=lambda k: _image_cache[k]["ts"])
            total -= len(_image_cache[oldest]["bytes"])
            del _image_cache[oldest]
    return dl_id


def get_cached_image(dl_id: str) -> dict | None:
    """返回缓存项 {bytes, fmt, size} 或 None（供 /api/imagedl 路由与 ImageApi 共用）。"""
    with _image_cache_lock:
        return _image_cache.get(dl_id)


def generate_image(api_key: str, prompt: str, size: str, model: str,
                   watermark: bool = False, prompt_extend: bool = True) -> dict:
    """调用 SenseNova U1.5 图片生成接口，返回 {b64, output_format, size}。
    使用 b64_json 直接取回图像数据，避免临时 URL 24 小时失效与 CSP 限制。"""
    if not api_key:
        raise ValueError("缺少 API Key")
    if not prompt or not prompt.strip():
        raise ValueError("提示词不能为空")
    if not _valid_image_size(size):
        raise ValueError("尺寸不合法（需 auto 或 WIDTHxHEIGHT，宽高为 32 倍数、512–4096、比例 ≤ 3:1）")
    payload = {
        "model": model or "sensenova-u1.5-fast",
        "prompt": prompt,
        "size": size or "auto",
        "n": 1,
        "watermark": bool(watermark),
        "prompt_extend": bool(prompt_extend),
        "response_format": "b64_json",
        "output_format": "png",
    }
    data = _post_image_api(api_key, IMAGE_GEN_ENDPOINT, payload)
    return _extract_image_data(data, size)


def edit_image(api_key: str, prompt: str, image_data_urls: list, size: str, model: str,
               watermark: bool = False, prompt_extend: bool = True) -> dict:
    """调用 SenseNova U1.5 图片编辑接口。
    image_data_urls 为 base64 data URL 列表，第 1 张为主编辑图，至多 5 张。"""
    if not api_key:
        raise ValueError("缺少 API Key")
    if not prompt or not prompt.strip():
        raise ValueError("编辑指令不能为空")
    if not image_data_urls:
        raise ValueError("请至少上传一张参考图")
    if len(image_data_urls) > 5:
        raise ValueError("参考图最多 5 张")
    if not _valid_image_size(size):
        raise ValueError("尺寸不合法（需 auto 或 WIDTHxHEIGHT，宽高为 32 倍数、512–4096、比例 ≤ 3:1）")
    # data URL 必须带完整 data:image/{format};base64, 前缀，否则被拒
    for u in image_data_urls:
        if not str(u).startswith("data:image/"):
            raise ValueError("图片数据格式不正确，需为 base64 data URL")
    payload = {
        "model": model or "sensenova-u1.5-fast",
        "images": [{"image_url": u} for u in image_data_urls],
        "prompt": prompt,
        "size": size or "auto",
        "n": 1,
        "watermark": bool(watermark),
        "prompt_extend": bool(prompt_extend),
        "response_format": "b64_json",
        "output_format": "png",
    }
    data = _post_image_api(api_key, IMAGE_EDIT_ENDPOINT, payload)
    return _extract_image_data(data, size)


def get_default_api_key(username: str) -> str:
    """取某账号的默认（或首个）Token Plan API Key 明文，用于图像生成等调用。"""
    _, _, keys = sensenova_api.resolve_account_keys(username)
    if not keys:
        raise ValueError("该账号暂无 API Key，请到 SenseNova 平台创建后刷新")
    # 优先取标记为默认的 Key，否则取第一个
    for k in keys:
        if isinstance(k, dict) and k.get("is_default") and k.get("api_key"):
            return str(k.get("api_key"))
    for k in keys:
        if isinstance(k, dict) and k.get("api_key"):
            return str(k.get("api_key"))
    raise ValueError("该账号的 API Key 均无明文值")


class ImageApi:
    """暴露给前端 window.pywebview.api 的下载接口。"""

    def open_url(self, url: str) -> dict:
        """用系统默认浏览器打开外链（用于「下载新版本」等）。"""
        import webbrowser
        try:
            webbrowser.open(url)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": accounts.sanitize_error(e, 120)}

    def save_image(self, dl_id: str) -> dict:
        item = get_cached_image(dl_id)
        if not item or not item.get("bytes"):
            return {"ok": False, "error": "图片已过期或不存在，请重新生成"}
        raw = item["bytes"]
        fmt = item.get("fmt") or "png"
        win = webview.windows[0] if getattr(webview, "windows", None) else None
        if not win:
            return {"ok": False, "error": "窗口未就绪"}
        try:
            result = win.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename="sensenova_image." + fmt,
                file_types=(f"{fmt.upper()} (*.{fmt})", "All files (*.*)"),
            )
        except Exception as e:
            return {"ok": False, "error": accounts.sanitize_error(e, 120)}
        path = None
        if isinstance(result, str):
            path = result
        elif isinstance(result, (list, tuple)) and result:
            path = result[0]
        if not path:
            return {"ok": False, "cancelled": True}
        try:
            with open(path, "wb") as f:
                f.write(raw)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": accounts.sanitize_error(e, 120)}
