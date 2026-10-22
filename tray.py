#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 系统托盘 + 后台刷新 + 单实例

关闭窗口不退出进程，隐藏到托盘继续后台刷新。
托盘图标按刷新结果变色（正常蓝/告警橙/错误红）。
单实例：会话级 named mutex + named event，重复启动唤起已有窗口。
"""

import os
import sys
import threading
import time

import accounts
import sensenova_api

# ---- 托盘图标 ----
_icon = None
_icon_thread = None
_paused = False
_window_hidden = False   # 窗口是否处于托盘隐藏态
_exiting = False         # 真正退出中，closing 事件不再拦截
_status_text = ""

# ---- 后台刷新 ----
_bg_thread = None
_bg_stop = threading.Event()
_bg_interval = 300
_refresh_lock = threading.Lock()   # 单飞：同一时刻只允许一次查询
_refreshing = False

# ---- 单实例 mutex / 唤起事件 ----
_MUTEX_NAME = "Local\\SenseNovaUsageDashboard"
_SHOW_EVENT = "Local\\SenseNovaUsageDashboard_show"
_mutex_handle = None
_show_event_handle = None
_show_listener_thread = None

# 颜色
_COLORS = {
    "normal": (9, 88, 217),    # #0958d9 蓝
    "alert": (173, 104, 0),   # #ad6800 橙
    "error": (245, 34, 45),   # #f5222d 红
}


def _create_icon_image(color):
    """用 Pillow 生成 64x64 圆形托盘图标。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, 58, 58], fill=color + (255,))
    draw.ellipse([20, 20, 44, 44], fill=(255, 255, 255, 200))
    return img


# ---------------- 窗口与前后台状态 ----------------

def tray_available():
    return _icon is not None


def is_paused():
    return _paused


def is_background():
    return _window_hidden


def exiting():
    return _exiting


def request_exit():
    """标记真正退出，供 closing 事件判断是否拦截。"""
    global _exiting
    _exiting = True


def set_window_hidden(hidden):
    """由 close_window / 显示窗口 / 隐藏启动统一维护隐藏态。"""
    global _window_hidden
    _window_hidden = bool(hidden)


def _notify_js(script):
    """向主窗口注入脚本；窗口/页面不存在时静默忽略。"""
    try:
        import webview
        if webview.windows:
            webview.windows[0].evaluate_js(script)
    except Exception:
        pass


def _show_window(refresh=True):
    """显示并聚焦主窗口，同时把刷新驱动权交回前端。"""
    import webview
    if not webview.windows:
        return
    stop_bg_refresh()
    set_window_hidden(False)
    w = webview.windows[0]
    try:
        w.show()
    except Exception:
        pass
    # 置前（show 不保证抢到焦点）
    _focus_existing_window()
    if refresh:
        # 前端恢复计时并立刻刷新一次
        _notify_js("setBackgroundMode(false)")


# ---------------- 托盘菜单 ----------------

def _on_show(icon, item):
    _show_window(refresh=True)


def _on_settings(icon, item):
    _show_window(refresh=True)
    _notify_js("showPage('settings')")


def _on_refresh(icon, item):
    """立即刷新：窗口可见时交给前端（与自动刷新同一路径），隐藏时后台查询。"""
    if not _window_hidden:
        import webview
        if webview.windows:
            try:
                webview.windows[0].evaluate_js("fetchUsage()")
                return
            except Exception:
                pass
    _do_refresh()


def toggle_pause():
    """暂停 / 恢复监控，菜单与设置页共用。"""
    global _paused
    _paused = not _paused
    if _paused:
        stop_bg_refresh()
        _notify_js("setMonitoringPaused(true)")
    else:
        _notify_js("setMonitoringPaused(false)")
        if _window_hidden:
            _do_refresh()
            start_bg_refresh(_bg_interval)
    # 图标颜色保持不变，只在悬浮提示里标注暂停
    _update_title()
    if _icon:
        try:
            _icon.update_menu()
        except Exception:
            pass


def _on_pause(icon, item):
    toggle_pause()


def _on_exit(icon, item):
    """托盘菜单：退出。"""
    request_exit()
    stop_bg_refresh()
    stop_tray()
    import webview
    if webview.windows:
        webview.windows[0].destroy()


def _build_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("打开主窗口", _on_show, default=True),
        pystray.MenuItem("立即刷新", _on_refresh,
                         enabled=lambda item: not _refreshing),
        pystray.MenuItem(
            lambda _: "恢复监控" if _paused else "暂停监控",
            _on_pause,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("设置", _on_settings),
        pystray.MenuItem("退出", _on_exit),
    )


# ---------------- 图标状态 ----------------

def _update_title():
    if not _icon:
        return
    parts = []
    if _status_text:
        parts.append(_status_text)
    if _paused:
        parts.append("监控已暂停")
    try:
        _icon.title = "SenseNova 用量查询" + (
            " · " + " · ".join(parts) if parts else ""
        )
    except Exception:
        pass


def _set_status(status, tooltip=None):
    """切换托盘图标颜色。status: normal / alert / error。"""
    global _status_text
    _status_text = tooltip or ""
    if not _icon:
        return
    try:
        _icon.icon = _create_icon_image(_COLORS.get(status, _COLORS["normal"]))
    except Exception:
        pass
    _update_title()


def update_from_result(result):
    """根据查询结果更新托盘状态；窗口内刷新也走这里，保证同一份状态。"""
    if not result:
        _set_status("error", "无数据")
        return
    accs = result.get("accounts", [])
    if not accs:
        _set_status("normal", "无账号")
        return

    has_error = False
    has_alert = False  # 额度耗尽 / 不可用

    for a in accs:
        if a.get("error"):
            has_error = True
            break
        for pool in a.get("items", []):
            try:
                pct5 = float(pool.get("usage_pct_5h", 0) or 0)
                pct7 = float(pool.get("usage_pct_7d", 0) or 0)
            except (TypeError, ValueError):
                continue
            if pct5 >= 100 or pct7 >= 100:
                has_alert = True

    if has_error:
        _set_status("error", "有账号查询失败")
    elif has_alert:
        _set_status("alert", "有账号额度告警")
    else:
        _set_status("normal", "监控正常")


def _do_refresh():
    """执行一次用量查询并更新托盘状态；同一时刻只允许一个在途查询。"""
    global _refreshing
    if not _refresh_lock.acquire(blocking=False):
        return
    _refreshing = True
    try:
        update_from_result(sensenova_api.query_all_accounts())
    except Exception:
        _set_status("error", "刷新失败")
    finally:
        _refreshing = False
        _refresh_lock.release()


# ---------------- 后台刷新 ----------------

def start_bg_refresh(interval=300):
    """启动后台刷新循环（窗口隐藏时用）。

    每次启动使用独立 Event，避免旧线程尚未退出时恢复监控失败。
    """
    global _bg_thread, _bg_stop, _bg_interval
    try:
        interval = int(interval or 300)
    except (TypeError, ValueError):
        interval = 300
    _bg_interval = min(max(interval, accounts.REFRESH_MIN), accounts.REFRESH_MAX)

    if _bg_thread and _bg_thread.is_alive() and not _bg_stop.is_set():
        return  # 已有循环在运行
    stop = threading.Event()
    _bg_stop = stop
    _bg_thread = threading.Thread(target=_bg_loop, args=(stop,), daemon=True)
    _bg_thread.start()


def stop_bg_refresh():
    """停止后台刷新循环；线程可能在本次查询结束后自行退出。"""
    _bg_stop.set()


def _bg_loop(stop):
    """后台刷新循环：按间隔调 query_all_accounts，更新托盘。"""
    while not stop.wait(_bg_interval):
        if _paused:
            continue
        _do_refresh()


def stop_bg_refresh_now():
    """等待在途后台线程退出（退出流程用），最多 5 秒。"""
    stop_bg_refresh()
    t = _bg_thread
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=5)


def update_interval(seconds):
    """设置变更后同步后台刷新间隔。"""
    global _bg_interval
    try:
        seconds = int(seconds or 300)
    except (TypeError, ValueError):
        seconds = 300
    _bg_interval = min(max(seconds, accounts.REFRESH_MIN), accounts.REFRESH_MAX)


# ---------------- 托盘生命周期 ----------------

def init_tray():
    """创建托盘图标并启动 daemon 线程。失败则静默降级（关闭即退出）。"""
    global _icon, _icon_thread
    try:
        import pystray
        _icon = pystray.Icon(
            "SenseNova用量查询",
            _create_icon_image(_COLORS["normal"]),
            "SenseNova 用量查询",
            _build_menu(),
        )
        _icon_thread = threading.Thread(target=_run_icon, daemon=True)
        _icon_thread.start()
    except Exception:
        _icon = None  # 托盘不可用，关闭行为回退为退出


def _run_icon():
    """pystray 消息循环；异常退出时标记托盘不可用，避免关闭后窗口再也打不开。"""
    global _icon
    icon = _icon
    try:
        icon.run()
    except Exception:
        if _icon is icon:
            _icon = None


def stop_tray():
    """停止托盘图标。"""
    global _icon
    icon = _icon
    _icon = None
    if icon:
        try:
            icon.stop()
        except Exception:
            pass


# ---------------- 单实例 ----------------

def _focus_existing_window():
    """按窗口标题唤起已有实例（事件通知失败时的兜底）。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, "SenseNova 用量查询")
        if not hwnd:
            return False
        u.ShowWindow(hwnd, 9 if u.IsIconic(hwnd) else 5)  # SW_RESTORE / SW_SHOW
        u.BringWindowToTop(hwnd)
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def _signal_existing_window():
    """通知已有实例显示窗口（named event，会话级）。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenEventW(0x0002, False, _SHOW_EVENT)  # EVENT_MODIFY_STATE
        if not handle:
            return False
        k32.SetEvent(handle)
        k32.CloseHandle(handle)
        return True
    except Exception:
        return False


def start_show_listener():
    """监听「唤起窗口」事件，供重复启动的第二个实例通知本实例。"""
    global _show_event_handle, _show_listener_thread
    if sys.platform != "win32" or _show_listener_thread:
        return
    try:
        import ctypes
        _show_event_handle = ctypes.windll.kernel32.CreateEventW(
            None, False, False, _SHOW_EVENT
        )
        if not _show_event_handle:
            return
        _show_listener_thread = threading.Thread(target=_show_loop, daemon=True)
        _show_listener_thread.start()
    except Exception:
        pass


def _show_loop():
    import ctypes
    k32 = ctypes.windll.kernel32
    while True:
        try:
            if k32.WaitForSingleObject(_show_event_handle, 0xFFFFFFFF) == 0:
                _show_window(refresh=True)
        except Exception:
            time.sleep(1)


def check_single_instance():
    """会话级 named mutex 单实例检查。

    返回 True 表示是首个实例；False 表示已有实例（已唤起并退出）。
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.SetLastError(0)
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
        err = k32.GetLastError()
        if not handle:
            return True  # 创建失败不阻止启动
        if err == 183:  # ERROR_ALREADY_EXISTS
            k32.CloseHandle(handle)
            if not _signal_existing_window():
                if not _focus_existing_window():
                    ctypes.windll.user32.MessageBoxW(
                        None, "应用已在运行，请从托盘恢复窗口。",
                        "SenseNova 用量查询", 0x40  # MB_ICONINFORMATION
                    )
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True  # 检查失败不阻止启动


# ---------------- 开机自启 ----------------

def _auto_start_command():
    """生成自启命令行：路径加引号，带 --hidden 以便静默驻留托盘。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --hidden'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw  # 源码模式用 pythonw，避免弹出黑色控制台
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.py")
    return f'"{exe}" "{script}" --hidden'


def set_auto_start(enabled):
    """写/删 Windows 注册表开机自启项。返回 (ok, error)。"""
    if sys.platform != "win32":
        return False, "仅支持 Windows"
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        app_name = "SenseNovaUsageDashboard"
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, app_name, 0, winreg.REG_SZ, _auto_start_command())
        else:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as k:
                try:
                    winreg.DeleteValue(k, app_name)
                except FileNotFoundError:
                    pass  # 不存在就当已删
        return True, None
    except Exception as e:
        return False, str(e)
