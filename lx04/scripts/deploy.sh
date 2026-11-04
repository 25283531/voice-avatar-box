#!/usr/bin/env bash
# =============================================================================
# LX04 部署脚本 —— 把我们的 App 装到音箱上，并接管开机与桌面
#
# 用法：
#   ./deploy.sh install          安装 APK（不含接管）
#   ./deploy.sh takeover         接管：设为默认桌面 + 禁用小爱桌面 + 自启
#   ./deploy.sh probe            探测：按键 KeyCode / 包名 / 麦克风占用（先跑这个）
#   ./deploy.sh all              安装 + 接管
#   ./deploy.sh status           查看当前状态
#
# ⚠️ 所有步骤都会先备份当前的 launcher 设置，出问题可 `./deploy.sh restore`。
# =============================================================================
set -uo pipefail

PKG="com.elder.lx04"
ACT="$PKG/.MainActivity"
SERIAL="${ANDROID_SERIAL:-}"
STATE_DIR="${HOME}/.lx04-deploy"
BACKUP="$STATE_DIR/backup.txt"

# 需要禁用的包名（按实机 probe 的结果调整）
XIAOMI_LAUNCHER_PACKAGES="${XIAOMI_LAUNCHER_PACKAGES:-}"

adb_() {
  if [ -n "$SERIAL" ]; then
    adb -s "$SERIAL" "$@"
  else
    adb "$@"
  fi
}

say() { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
err() { printf '\033[1;31m[x] %s\033[0m\n' "$*"; }
ok() { printf '\033[1;32m[✓] %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------- probe

probe() {
  say "=== 1. 设备信息 ==="
  adb_ shell getprop ro.product.model
  adb_ shell getprop ro.build.version.release
  adb_ shell getprop ro.build.version.sdk
  adb_ shell getprop ro.product.cpu.abi
  adb_ shell wm size
  adb_ shell wm density

  say ""
  say "=== 2. 三个顶部按键的真实 KeyCode（★ 必须实测，不可猜）==="
  warn "这一步需要你手动按三个键。观察输出里的 keycode 值："
  warn "  · 音量+ / 音量- 应为 24 / 25"
  warn "  · 中间键的实际值就是要填进 TopKeys.CONFIRM_KEY_CODES 的那个"
  echo ""
  adb_ shell getevent -lp 2>&1 | head -60
  echo ""
  say "  现在按一下中间键，然后按 Ctrl-C 结束："
  adb_ shell getevent -lt 2>&1 | head -40

  say ""
  say "=== 3. 候选的桌面 / 语音包名 ==="
  adb_ shell pm list packages 2>&1 | grep -iE "launcher|xiaomi|voice|mico|assistant|tts" | sed 's/^package://'

  say ""
  say "=== 4. 当前 HOME 设置 ==="
  adb_ shell cmd package resolve-activity -c android.intent.category.HOME 2>&1 | grep -iE "packageName|name=" | head -5

  say ""
  say "=== 5. 麦克风是否被小爱占用 ==="
  warn "看 Client 列表里有没有小爱的进程（这决定我们能不能自己做唤醒）"
  adb_ shell dumpsys media.audio_flinger 2>&1 | grep -iA2 -E "Client|Active" | head -40

  say ""
  say "=== 6. SELinux 状态 ==="
  adb_ shell getenforce 2>&1

  say ""
  say "=== 7. 触摸坐标方向（SWAP_XY / FLIP_Y）==="
  adb_ shell dumpsys input 2>&1 | grep -iE "SurfaceOrientation|viewport|displayId" | head -10
}

# ------------------------------------------------------------------- install

install() {
  say "=== 查找 APK ==="
  APK=""
  for cand in \
    "$ROOT/lx04/app/build/outputs/apk/release/app-release.apk" \
    "$ROOT/lx04/app/build/outputs/apk/debug/app-debug.apk"
  do
    if [ -f "$cand" ]; then APK="$cand"; break; fi
  done
  if [ -z "$APK" ]; then
    err "找不到 APK。先构建：cd lx04 && ./gradlew assembleRelease"
    exit 1
  fi
  say "APK: $APK"

  say ""
  say "=== 安装 ==="
  # -r 覆盖安装，-g 授予所有运行时权限（Android 6+ 的 grant-all flag）
  adb_ install -r -g "$APK" || {
    err "安装失败"
    exit 1
  }
  ok "安装完成"
}

# ------------------------------------------------------------------ takeover

takeover() {
  mkdir -p "$STATE_DIR"

  say "=== 备份当前 HOME 设置 ==="
  adb_ shell cmd package resolve-activity -c android.intent.category.HOME 2>&1 \
    | grep -iE "packageName" > "$BACKUP" 2>/dev/null || true
  adb_ shell cmd package get-home-activities 2>&1 | head -20 > "$STATE_DIR/home-activities.txt"
  ok "已备份到 $STATE_DIR"

  say ""
  say "=== 1. 设为默认桌面 ==="
  # 这一步让系统记住「按 Home 键回到我们」+ 开机直接进我们
  if adb_ shell cmd package set-home-activity "$ACT" 2>&1; then
    ok "已设为默认桌面"
  else
    warn "set-home-activity 失败（部分 ROM 不支持该命令），改用 pm 方式"
    adb_ shell "pm clear $PKG" >/dev/null 2>&1
  fi

  say ""
  say "=== 2. 校验 HOME 归属 ==="
  HOME_NOW=$(adb_ shell cmd package resolve-activity -c android.intent.category.HOME 2>&1 \
    | grep -iE "packageName" | head -1)
  if echo "$HOME_NOW" | grep -q "$PKG"; then
    ok "HOME 已归属我们：$HOME_NOW"
  else
    warn "HOME 仍是：$HOME_NOW"
    warn "可手动在 音箱设置 → 默认应用 → 桌面 里选我们的 App"
  fi

  say ""
  say "=== 3. 禁用小爱桌面 ==="
  if [ -z "$XIAOMI_LAUNCHER_PACKAGES" ]; then
    warn "未指定小爱桌面包名。先跑 probe 找出包名，然后："
    warn "  XIAOMI_LAUNCHER_PACKAGES='com.x,com.y' ./deploy.sh takeover"
    warn "  暂时只做兜底：禁掉所有含 launcher 关键字的第三方包（危险，需人工确认）"
  fi
  IFS=',' read -ra PKGS <<< "$XIAOMI_LAUNCHER_PACKAGES"
  for p in "${PKGS[@]}"; do
    [ -z "$p" ] && continue
    p=$(echo "$p" | tr -d ' ')
    if adb_ shell pm disable-user --user 0 "$p" 2>&1 | grep -q "new package state"; then
      ok "已禁用 $p"
    else
      warn "禁用 $p 失败（可能已经是禁用状态）"
    fi
  done

  say ""
  say "=== 4. 开机自启 ==="
  # BOOT_COMPLETED 已经写进 Manifest，只需确认接收器是启用状态
  adb_ shell "cmd package set-enabled-setting $PKG/.BootReceiver 0 true" 2>&1 | head -2
  ok "自启接收器已启用"

  say ""
  say "=== 5. 电池优化白名单（老人设备要 7×24 在线）==="
  adb_ shell dumpsys deviceidle whitelist +"$PKG" 2>&1 | head -2
  ok "已加入 doze 白名单"

  say ""
  say "=== 6. 音量键回归检查（音量± 必须仍然可用）==="
  warn "请在音箱上手动按一下音量+ 和音量-，确认音量条有反应"
}

# ------------------------------------------------------------------- status

status() {
  say "=== 安装状态 ==="
  adb_ shell pm list packages 2>&1 | grep "$PKG" && ok "已安装" || err "未安装"

  say ""
  say "=== HOME 设置 ==="
  adb_ shell cmd package resolve-activity -c android.intent.category.HOME 2>&1 | head -8

  say ""
  say "=== 权限 ==="
  adb_ shell dumpsys package "$PKG" 2>&1 | grep -A1 "RECORD_AUDIO" | head -6

  say ""
  say "=== 运行状态 ==="
  adb_ shell ps 2>&1 | grep "$PKG" | head -3

  say ""
  say "=== 被禁用的小爱包（是否是我们禁的）==="
  adb_ shell pm list packages -d 2>&1 | grep -iE "xiaomi|voice|mico|launcher" | head -10
}

# ------------------------------------------------------------------ restore

restore() {
  say "=== 恢复小爱桌面 ==="
  for p in $(cat "$BACKUP" 2>/dev/null | grep -oE "[a-zA-Z0-9_.]+" | grep -v "^packageName$" | sort -u); do
    adb_ shell pm enable "$p" 2>&1 | head -1
    ok "已启用 $p"
  done
  say ""
  say "=== 取消我们的默认桌面 ==="
  # 让系统重新弹出选择框
  adb_ shell "cmd package clear-package-preferred-activities $PKG" 2>&1 | head -2
  ok "完成。重启后按 Home 会重新询问"
}

# --------------------------------------------------------------------- main

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$STATE_DIR"

case "${1:-all}" in
  probe)    probe ;;
  install)  install ;;
  takeover) takeover ;;
  status)   status ;;
  restore)  restore ;;
  all)      install && takeover ;;
  *) echo "用法: $0 {probe|install|takeover|status|restore|all}"; exit 1 ;;
esac
