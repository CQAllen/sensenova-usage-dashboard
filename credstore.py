#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 凭据安全存储模块

职责：
  - 决定配置目录：迁出仓库 / 云同步盘；打包（frozen）态不再写入 .app bundle（否则破坏签名）
  - 用 Fernet 加密 accounts.json 内容，密钥由平台原生机制保护：
        Windows → DPAPI（CryptProtectData，绑定当前用户）
        POSIX   → 0600 权限的密钥文件
  - 原子写入（临时文件 + os.replace），POSIX 下 chmod 0600
  - 兼容读取旧的明文 accounts.json，并在读取时自动迁移为密文

信封格式（自描述，便于将来更换算法）：
    {"v": 1, "scheme": "dpapi" | "keyfile", "data": "<Fernet token>"}

安全边界：加密只防"配置文件被拷走 / 误提交 / 云同步外泄"，
对同一登录用户下的本机进程无效（它同样能读到进程内存与密钥）。
"""

import base64
import json
import os
import secrets
import sys
import tempfile

ACCOUNTS_FILE = "accounts.json"
KEY_FILE = "accounts.key"        # 密钥文件名（*.key 已在 .gitignore 中忽略）
ENVELOPE_VERSION = 1

try:
    from cryptography.fernet import Fernet
    _HAVE_FERNET = True
except Exception:               # 缺少加密组件时优雅降级为明文 + 告警
    Fernet = None
    _HAVE_FERNET = False


# ============================================================
# 配置目录
# ============================================================
_SYNC_MARKERS = (
    "onedrive", "dropbox", "google drive", "googledrive", "google-drive",
    "icloud", "nutstore", "坚果云", "baidunetdisk", "百度网盘", "weiyun", "微云",
)


def _is_sync_path(path):
    p = path.lower()
    return any(m in p for m in _SYNC_MARKERS)


def user_config_dir():
    """跨平台的用户级配置目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "SenseNova用量查询")


def resolve_config_dir():
    """返回配置目录。打包态一律用用户级目录；源码态默认脚本目录，
    但若脚本目录位于云同步盘/网盘则改存用户级目录，避免凭据被同步外泄。"""
    if getattr(sys, "frozen", False):
        return user_config_dir()
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if _is_sync_path(script_dir):
        return user_config_dir()
    return script_dir


def accounts_path(config_dir):
    return os.path.join(config_dir, ACCOUNTS_FILE)


# ============================================================
# 原子写入 / 隔离
# ============================================================
def _atomic_write(path, text):
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".acc-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _quarantine(path):
    """把无法解密/解析的文件挪到 .bad，避免阻塞启动。"""
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass


# ============================================================
# 密钥保护
# ============================================================
def _key_path(config_dir):
    return os.path.join(config_dir, KEY_FILE)


def _read_key_raw(config_dir):
    with open(_key_path(config_dir), "rb") as f:
        return f.read()


def _write_key_raw(config_dir, blob):
    path = _key_path(config_dir)
    fd, tmp = tempfile.mkstemp(dir=config_dir, prefix=".key-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _dpapi(data, unprotect=False):
    """调用 Windows DPAPI 保护/还原数据（系统 crypt32.dll，PyInstaller 下可用）。"""
    import ctypes

    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    def _make(b):
        buf = ctypes.create_string_buffer(b, len(b))
        return _BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    blob_in, _keep = _make(data)
    blob_out = _BLOB()
    fn = (ctypes.windll.crypt32.CryptUnprotectData if unprotect
          else ctypes.windll.crypt32.CryptProtectData)
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise OSError("DPAPI 调用失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _fernet(raw_key):
    return Fernet(base64.urlsafe_b64encode(raw_key))


def _get_key(config_dir):
    """返回 (fernet_or_None, scheme)。密钥损坏时丢弃旧密钥并重建（旧密文由上层隔离）。"""
    if not _HAVE_FERNET:
        return None, "plain"
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError:
        return None, "plain"

    kp = _key_path(config_dir)

    if sys.platform == "win32":
        try:
            if os.path.exists(kp):
                raw = _dpapi(_read_key_raw(config_dir), unprotect=True)
                if len(raw) != 32:
                    raise ValueError("密钥长度异常")
            else:
                raw = secrets.token_bytes(32)
                _write_key_raw(config_dir, _dpapi(raw))
            return _fernet(raw), "dpapi"
        except Exception:
            # 换了系统用户 / 机器，或密钥损坏：重建密钥
            _quarantine(kp)
            try:
                raw = secrets.token_bytes(32)
                _write_key_raw(config_dir, _dpapi(raw))
                return _fernet(raw), "dpapi"
            except Exception:
                return None, "plain"

    # POSIX
    try:
        if os.path.exists(kp):
            raw = _read_key_raw(config_dir)
            key = raw if len(raw) == 32 else base64.urlsafe_b64decode(raw)
        else:
            key = secrets.token_bytes(32)
            _write_key_raw(config_dir, key)
        return _fernet(key), "keyfile"
    except Exception:
        _quarantine(kp)
        try:
            key = secrets.token_bytes(32)
            _write_key_raw(config_dir, key)
            return _fernet(key), "keyfile"
        except Exception:
            return None, "plain"


# ============================================================
# 文档读写
# ============================================================
def _is_envelope(doc):
    return (isinstance(doc, dict) and doc.get("v") == ENVELOPE_VERSION
            and isinstance(doc.get("data"), str) and "scheme" in doc)


def write_document(config_dir, obj):
    """加密（或降级为明文）写回配置文档，返回采用的 scheme。"""
    os.makedirs(config_dir, exist_ok=True)
    path = accounts_path(config_dir)
    fernet, scheme = _get_key(config_dir)
    if fernet is not None:
        token = fernet.encrypt(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
        content = json.dumps(
            {"v": ENVELOPE_VERSION, "scheme": scheme, "data": token.decode("utf-8")},
            ensure_ascii=False,
        )
    else:
        content = json.dumps(obj, ensure_ascii=False, indent=2)
    _atomic_write(path, content)
    return scheme


def read_document(config_dir):
    """读取配置文档，返回 (obj_or_None, warning)。

    - 新信封：解密后返回；解密失败则隔离旧文件并返回 None + 告警
    - 旧明文文件：解析后立即迁移为密文，返回 obj + 迁移告警
    - 文件不存在：返回 (None, None)
    """
    path = accounts_path(config_dir)
    if not os.path.exists(path):
        return None, None

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except Exception as e:
        return None, f"读取账号配置失败：{e}"

    try:
        doc = json.loads(raw)
    except Exception:
        doc = None

    if _is_envelope(doc):
        fernet, _scheme = _get_key(config_dir)
        if fernet is None:
            return None, ("缺少加密组件，无法解密已加密的账号配置；"
                          "请安装 cryptography 后重试。")
        try:
            plain = fernet.decrypt(doc["data"].encode("utf-8"))
            obj = json.loads(plain.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("内容不是对象")
            return obj, None
        except Exception:
            _quarantine(path)
            return None, ("账号配置解密失败（可能更换了系统用户/机器或密钥损坏），"
                          "已隔离旧文件为 .bad，请重新填写账号密码。")

    # 旧明文文件
    if isinstance(doc, dict):
        warn = None
        fernet, scheme = _get_key(config_dir)
        if fernet is not None:
            try:
                write_document(config_dir, doc)
                warn = ("已将明文账号配置迁移为加密存储；"
                        "旧明文可能仍残留在备份、云同步盘或 git 历史中。")
            except Exception:
                warn = "账号配置迁移为加密存储失败，当前仍以明文保存。"
        else:
            warn = "未能加密账号配置（缺少加密组件），当前仍以明文保存。"
        return doc, warn

    _quarantine(path)
    return None, "账号配置文件格式无法识别，已隔离为 .bad。"
