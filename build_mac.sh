#!/bin/bash
# ============================================================
# macOS 一键打包脚本 —— 把程序打成独立 .app（免装 Python 即可双击运行）
# ⚠️ 必须在 macOS 上运行（PyInstaller 无法在 Windows/Linux 上交叉编译 Mac 包）
#
# 前置要求（只需首次）：
#   1. 安装「framework 版」Python（系统自带的 /usr/bin/python3 不行！）
#       推荐： brew install python        （Homebrew 版就是 framework 构建）
#       或去 https://python.org 下载安装包
#   2. 本脚本会自动 pip 安装依赖（含 pyobjc，pywebview 在 Mac 的 Cocoa 后端依赖它）
#
# 用法：
#   chmod +x build_mac.sh
#   ./build_mac.sh
#
# 产物： dist/SenseNova用量查询.app   ← 这就是可分发应用
#        双击即可运行；可拖进「应用程序」文件夹长期使用
#        凭据（加密后的 accounts.json + 密钥）保存在
#        ~/Library/Application Support/SenseNova用量查询/ 下，不会写进 .app
# ============================================================

set -e
cd "$(dirname "$0")"

# 图标必须存在（生成脚本 make_icon_mac.py 已移除，请直接用仓库里的 app.icns）
if [ ! -f "app.icns" ]; then
  echo "------------------------------------------------------------"
  echo "⚠️  未找到 app.icns，且图标生成脚本已移除！"
  echo "请先把 app.icns 放到本目录再打包。"
  echo "------------------------------------------------------------"
  exit 1
fi

PYTHON=$(command -v python3 || command -v python)
echo "当前 Python: $PYTHON"

# 检测是否为 framework 构建（pywebview 在 macOS 上必须有 framework Python，否则运行时崩溃）。
# 用 sysconfig 的 PYTHONFRAMEWORK 配置判断，比路径字串更可靠（兼容 python.org / Homebrew / conda-forge 等所有 framework build）。
# conda / miniforge 环境的 PYTHONFRAMEWORK 为空，不能直接用。
if ! "$PYTHON" -c "import sysconfig; assert sysconfig.get_config_var('PYTHONFRAMEWORK'), 'PYTHONFRAMEWORK is empty'" 2>/dev/null; then
  echo "------------------------------------------------------------"
  echo "⚠️  当前 Python 不是 framework 构建，pywebview 无法运行！"
  echo "  Python: $PYTHON"
  echo "  （conda / miniforge 环境均非 framework 构建，不适用）"
  echo "正在寻找系统上的 framework 版 Python ..."

  # 自动搜索 framework 版 Python
  _found=""
  for cmd in python3 python /usr/local/bin/python3 /opt/homebrew/bin/python3 \
             /Library/Frameworks/Python.framework/Versions/3.*/bin/python3; do
    p=$(command -v "$cmd" 2>/dev/null) || p="$cmd"
    [ -x "$p" ] || continue
    if "$p" -c "import sysconfig; assert sysconfig.get_config_var('PYTHONFRAMEWORK')" 2>/dev/null; then
      _found="$p"
      break
    fi
  done

  if [ -n "$_found" ]; then
    echo "找到 framework 版 Python: $_found"
    PYTHON="$_found"
  else
    echo "未找到 framework 版 Python。请任选其一安装后重试："
    echo "  1) Homebrew（推荐）: brew install python"
    echo "  2) python.org 安装包: https://www.python.org/downloads/"
    echo "------------------------------------------------------------"
    exit 1
  fi
  echo "------------------------------------------------------------"
fi

echo "使用 Python: $PYTHON"

echo "安装/更新依赖..."
# 分两次装：先把 pyobjc 全家桶（编译重头）装完，再装剩下的轻包。
# 拆开是为了避开 pip 25.x 在装多个 sdist 包时并发 build 的 EEXIST bug。
"$PYTHON" -m pip install --quiet --upgrade --default-timeout=300 \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit

# ⚠️ cryptography 不能 --upgrade：49+ 在 macOS x86_64 + Python 3.13 framework 上没有预编译
# wheel，pip 会尝试从源码编译（需要 Rust），大概率失败或极慢。48.x 有现成 wheel，直接用。
# jwcrypto 同理，锁 1.5.8（兼容 cryptography 48.x）；1.6.0 会要求 cryptography>=49。
# --upgrade-strategy only-if-needed 防止 pip 自作主张升级已有满足依赖的包。
"$PYTHON" -m pip install --quiet --upgrade-strategy only-if-needed --default-timeout=300 \
  requests "cryptography<49" "jwcrypto==1.5.8" pywebview pyinstaller

APP="SenseNova用量查询"
APP_BUNDLE="dist/$APP.app"

echo "开始打包（onedir + windowed → 生成 .app，图标 app.icns）..."

"$PYTHON" -m PyInstaller --noconfirm --onedir --windowed \
  --name "$APP" \
  --icon "app.icns" \
  --osx-bundle-identifier "com.sensenova.usage" \
  --collect-all webview \
  --hidden-import webview.platforms.cocoa \
  --hidden-import pyobjc \
  --hidden-import requests \
  --hidden-import jwcrypto \
  --hidden-import cryptography \
  --hidden-import auth_login \
  --hidden-import credstore \
  dashboard.py

# 版本号须与 dashboard.py 的 APP_VERSION 保持一致
APP_VERSION="1.2.0"

echo "写入版本信息到 Info.plist..."
PLIST="$APP_BUNDLE/Contents/Info.plist"
if [ -f "$PLIST" ]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $APP_VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string $APP_VERSION" "$PLIST"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $APP_VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $APP_VERSION" "$PLIST"
  /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile app" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string app" "$PLIST"
  echo "  版本号已设为 $APP_VERSION"
fi

echo "尝试 ad-hoc 签名（绕过 Gatekeeper 拦截，失败不影响使用）..."
codesign --force --deep --sign - "$APP_BUNDLE" 2>/dev/null \
  && echo "  已签名" \
  || echo "  （可选）codesign 不可用，跳过；首次打开请右键 → 打开"

echo ""
echo "✅ 打包完成！"
echo "   应用： $(pwd)/$APP_BUNDLE"
echo "   使用：双击 .app 即可；或拖入「应用程序」文件夹长期使用"
echo "   若提示「无法验证开发者」：右键 .app → 打开，即可永久放行"
echo "   配置：窗口内展开「账号配置」→ 填用户名 + 密码 → 点登录"
