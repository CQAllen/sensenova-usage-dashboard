#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Windows 一键打包 —— PyInstaller onefile，产出 dist/SenseNova用量查询.exe。

版本号单一来源：dashboard.py 的 APP_VERSION。本脚本读取它并生成 version_info.txt，
避免版本号散落在多处手动同步。

用法（在 Windows 上、已装好依赖的 .venv 或系统 Python 里）：
    python build_win.py

前置：requirements.txt 已装（含 pywebview→pythonnet），并 pip install pyinstaller。
产物：dist/SenseNova用量查询.exe（单文件，双击即运行，无需装 Python）。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

# ---- 从 dashboard.py 读 APP_VERSION（单一来源，不 import 避免触发启动期自检）----
src = open("dashboard.py", encoding="utf-8").read()
m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', src)
if not m:
    print("[错误] 未能从 dashboard.py 解析 APP_VERSION", file=sys.stderr)
    sys.exit(1)
APP_VERSION = m.group(1)
print(f"APP_VERSION = {APP_VERSION}")

vers = APP_VERSION.split(".")
while len(vers) < 3:
    vers.append("0")
vers_tuple = ", ".join(vers + ["0"])  # filevers 需要 4 段

# ---- 生成 version_info.txt（Windows 文件属性面板里显示的版本/产品名）----
version_info = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({vers_tuple}),
    prodvers=({vers_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
      StringTable(
        '080404B0',
        [StringStruct('CompanyName', ''),
         StringStruct('FileDescription', 'SenseNova 用量查询'),
         StringStruct('FileVersion', '{APP_VERSION}.0'),
         StringStruct('InternalName', 'SenseNova用量查询'),
         StringStruct('LegalCopyright', 'Copyright (C) 2026'),
         StringStruct('OriginalFilename', 'SenseNova用量查询.exe'),
         StringStruct('ProductName', 'SenseNova 用量查询'),
         StringStruct('ProductVersion', '{APP_VERSION}.0')])
      ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
with open("version_info.txt", "w", encoding="utf-8") as f:
    f.write(version_info)
print("已生成 version_info.txt")

# 用固定的 Windows spec（sensenova_win.spec），不再用 CLI 参数现场生成 spec：
# 否则 --specpath 会让 --add-data 的相对路径解析错位（去找 build/web），
# 还会覆盖仓库根目录里 macOS 专用的 SenseNova用量查询.spec。
cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "sensenova_win.spec"]
print(" ".join(cmd))
subprocess.check_call(cmd)

out = os.path.join(ROOT, "dist", "SenseNova用量查询.exe")
print(f"\n[OK] 打包完成：{out}")
print("   双击即可运行（无需安装 Python）；分发把这一个 exe 发给用户即可。")
