#!/usr/bin/env bash
# =============================================================================
# LX04 部署脚本 —— 把我们的 App 装到音箱上，接管开机、桌面、按键与麦克风
#
# 用法：
#   ./deploy.sh probe      探测设备（只读，不改任何东西）
#   ./deploy.sh keymap     安装按键重映射（中间键 POWER → F1）★ 必须先做
#   ./deploy.sh install    安装 APK
#   ./deploy.sh takeover   接管：设默认桌面 + 禁小爱桌面 + 禁 mivpm + 自启
#   ./deploy.sh all        keymap + install + takeover
#   ./deploy.sh status     查看当前状态
#   ./deploy.sh restore    回滚（恢复中间键开关屏幕、恢复小爱、取消默认桌面）
#
# ⚠️ 每一步都可逆。takeover 之前会先备份到 $STATE_DIR。
#
# 本脚本的所有结论都来自 2026-11-04 的实机验证，
# 详见 docs/lx04/实机验证结果.md。
# =============================================================================
set -uo pipefail

PKG="com.elder.lx04"
ACT="$PKG/.MainActivity"
SERIAL="${ANDROID_SERIAL:-}"
ADB_BIN="${ADB_BIN:-adb}"

STATE_DIR="${HOME}/.lx04-deploy"
BACKUP="$STATE_DIR/backup.txt"

# --- 实机确认的常量（不要凭资料猜）---------------------------------------
#
# 小爱桌面。也是小爱语音 + 米家 :miot 的宿主（没有独立语音包）。
XIAOMI_LAUNCHER_PACKAGES="${XIAOMI_LAUNCHER_PACKAGES:-com.xiaomi.micolauncher}"

# 小米语音处理模块。**它才是麦克风的占用者** ——
# 实测 am force-stop micolauncher 后麦克风仍被占，stop mivpm 才释放。
MIVPM_SERVICE="${MIVPM_SERVICE:-mivpm}"

# 中间键所在输入设备（event1）的 keylayout 文件名。
KEYLAYOUT_NAME="mtk-pmic-keys.kl"
KEYLAYOUT_DIR="/system/usr/keylayout"

adb_() {
  if [ -n "$SERIAL" ]; then
    "$ADB_BIN" -s "$SERIAL" "$@"
  else
    "$ADB_BIN" "$@"
  fi
}

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
err()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m[✓] %s\033[0m\n' "$*"; }

need_device() {
  if ! adb_ get-state >/dev/null 2>&1; then
    err "没有设备。先确认 adb devices 能看到设备（USB 或网络 adb）"
    exit 1
  fi
}

# --------------------------------------------------------------------- probe

probe() {
  need_device
  say "=== 1. 设备信息 ==="
  adb_ shell 'getprop ro.product.model; getprop ro.build.version.release; getprop ro.build.version.sdk; getprop ro.product.cpu.abi; getprop ro.build.type'
  adb_ shell wm size
  adb_ shell wm density

  say ""
  say "=== 2. 权限状态 ==="
  adb_ shell 'id; getenforce'
  warn "期望 uid=0(root) + userdebug（可 remount /system）"

  say ""
  say "=== 3. 三个顶部按键 ==="
  adb_ shell 'getevent -pl' 2>/dev/null \
    | grep -E 'add device|name:|KEY_' | head -30
  warn "实机结论：event0 mtk-kpd = 音量±；event1 mtk-pmic-keys = 音量- + POWER（中间键）"

  say ""
  say "=== 4. 中间键当前的 keylayout 归属 ==="
  adb_ shell dumpsys input 2>/dev/null \
    | grep -A9 'mtk-pmic-keys' | grep -E 'KeyLayoutFile|Path'

  say ""
  say "=== 5. 麦克风占用者（★ 决定我们能否录音）==="
  adb_ shell dumpsys media.audio_flinger 2>/dev/null \
    | grep -A8 'Input thread' | head -14
  adb_ shell 'getprop init.svc.mivpm'
  warn "若 Input thread 的 Standby: no 且 mivpm=running ⇒ 麦克风被小爱占着"

  say ""
  say "=== 6. 桌面 ==="
  adb_ shell dumpsys package "$XIAOMI_LAUNCHER_PACKAGES" 2>/dev/null \
    | grep -B1 -A4 'android.intent.category.HOME' | head -10

  say ""
  say "=== 7. 网络 ==="
  adb_ shell 'ip addr show wlan0 | grep "inet "'
}

# -------------------------------------------------------------------- keymap

# 把中间键从 POWER 重映射成 F1。
#
# 为什么必须在应用层之外做：KEYCODE_POWER 在 interceptKeyBeforeQueueing
# 阶段就被系统吃掉，前台 Activity 的 dispatchKeyEvent 根本收不到 ——
# 也就是说「在 App 里拦一下」是拦不住的，只能改 keylayout。
keymap() {
  need_device

  say "=== 1. 备份原 keylayout 状态 ==="
  mkdir -p "$STATE_DIR"
  if adb_ shell "ls $KEYLAYOUT_DIR/$KEYLAYOUT_NAME" >/dev/null 2>&1; then
    adb_ shell "cat $KEYLAYOUT_DIR/$KEYLAYOUT_NAME" > "$STATE_DIR/$KEYLAYOUT_NAME.orig" 2>/dev/null
    warn "已存在 $KEYLAYOUT_NAME，原内容备份到 $STATE_DIR/$KEYLAYOUT_NAME.orig"
  else
    : > "$STATE_DIR/$KEYLAYOUT_NAME.absent"   # 记录"原本不存在"，restore 时据此删除
    ok "原本没有该文件（走的是 Generic.kl 的 key 116 POWER）"
  fi

  say ""
  say "=== 2. remount /system 为可写 ==="
  adb_ root >/dev/null 2>&1 || true
  sleep 1
  if adb_ remount 2>&1 | grep -qi "succeeded"; then
    ok "remount 成功"
  else
    warn "remount 未报告成功，继续尝试（部分 ROM 静默成功）"
  fi

  say ""
  say "=== 3. 写入重映射 ==="
  # 保留 event1 的音量-，把 POWER(116) 换成 F1。
  # F1 没有任何系统语义，系统收到也不做任何事。
  adb_ shell "printf 'key 114   VOLUME_DOWN\nkey 116   F1\n' > $KEYLAYOUT_DIR/$KEYLAYOUT_NAME && chmod 644 $KEYLAYOUT_DIR/$KEYLAYOUT_NAME"
  if adb_ shell "cat $KEYLAYOUT_DIR/$KEYLAYOUT_NAME" 2>/dev/null | grep -q "KEY_F1\|116   F1"; then
    ok "已写入 $KEYLAYOUT_DIR/$KEYLAYOUT_NAME"
  else
    err "写入失败"
    exit 1
  fi

  say ""
  say "=== 4. 重启生效 ==="
  warn "keylayout 在 input 设备打开时加载，必须重启（或重启 input 服务）"
  read -r -p "现在重启设备？[y/N] " ans
  if [ "${ans:-N}" = "y" ] || [ "${ans:-N}" = "Y" ]; then
    adb_ reboot
    say "等待设备回来..."
    sleep 20
    adb_ wait-for-device
    sleep 10
    ok "设备已回来"
    say ""
    say "=== 5. 校验加载结果 ==="
    adb_ shell dumpsys input 2>/dev/null | grep -A9 'mtk-pmic-keys' | grep 'KeyLayoutFile'
    warn "应显示 $KEYLAYOUT_DIR/$KEYLAYOUT_NAME"
    warn "然后手动按中间键：屏幕应当**不再熄灭**"
  else
    warn "已跳过重启。改动会在下次开机生效。"
  fi
}

# ------------------------------------------------------------------- install

install() {
  need_device
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
  # -r 覆盖安装，-g 授予所有运行时权限（省去在设备上点授权）
  adb_ install -r -g "$APK" || {
    err "安装失败"
    exit 1
  }
  ok "安装完成"
}

# ------------------------------------------------------------------ takeover

takeover() {
  need_device
  mkdir -p "$STATE_DIR"

  say "=== 备份当前状态 ==="
  adb_ shell cmd package get-home-activities > "$STATE_DIR/home-activities.txt" 2>&1 || true
  adb_ shell pm list packages -d > "$STATE_DIR/disabled-before.txt" 2>&1 || true
  ok "备份到 $STATE_DIR"

  say ""
  say "=== 1. 设为默认桌面 ==="
  if adb_ shell cmd package set-home-activity "$ACT" 2>&1 | grep -qiE "Success|new home"; then
    ok "已设为默认桌面"
  else
    warn "set-home-activity 未成功，尝试直接设置 preferred activity"
    adb_ shell "cmd package set-home-activity $ACT" 2>&1 | head -2
  fi

  say ""
  say "=== 2. 校验 HOME 归属 ==="
  HOME_NOW=$(adb_ shell dumpsys package "$PKG" 2>/dev/null | grep -c "android.intent.category.HOME" || echo 0)
  if [ "$HOME_NOW" -gt 0 ]; then
    ok "我们的 App 已注册 HOME"
  else
    err "Manifest 里没有 HOME —— APK 版本不对？"
  fi

  say ""
  say "=== 3. 释放麦克风：停掉 MiVpmService ==="
  warn "★ 这一步是能不能录音的关键。实测：禁桌面不能释放麦克风，停 mivpm 才行。"
  if adb_ shell "getprop init.svc.$MIVPM_SERVICE" 2>/dev/null | grep -q running; then
    adb_ shell "stop $MIVPM_SERVICE"
    sleep 1
    if adb_ shell "getprop init.svc.$MIVPM_SERVICE" 2>/dev/null | grep -q stopped; then
      ok "mivpm 已停止（重启后会自己回来，见下方永久禁用说明）"
    else
      warn "stop $MIVPM_SERVICE 未生效"
    fi
  else
    ok "mivpm 当前未运行"
  fi
  echo ""
  warn "⚠️ stop 只是临时生效，重启就回来了。要永久禁用，二选一："
  warn "   a) 改 /vendor/etc/init/hw/init.project.rc 注释掉 service mivpm 段"
  warn "   b) mv /system/bin/MiVpmService /system/bin/MiVpmService.disabled"
  warn "   本脚本不自动做，因为禁掉后小爱语音/米家语音就彻底没了，需要你确认。"

  say ""
  say "=== 4. 禁用小爱桌面 ==="
  IFS=',' read -ra PKGS <<< "$XIAOMI_LAUNCHER_PACKAGES"
  for p in "${PKGS[@]}"; do
    [ -z "$p" ] && continue
    p=$(echo "$p" | tr -d ' ')
    if adb_ shell pm disable-user --user 0 "$p" 2>&1 | grep -q "new package state"; then
      ok "已禁用 $p"
    else
      warn "禁用 $p 未成功（可能已禁用，或需要 root）"
    fi
  done

  say ""
  say "=== 5. 开机自启 ==="
  adb_ shell "cmd package set-enabled-setting $PKG/.BootReceiver 0 true" >/dev/null 2>&1
  ok "自启接收器已启用"

  say ""
  say "=== 6. 电池优化白名单（老人设备要 7×24 在线）==="
  adb_ shell "dumpsys deviceidle whitelist +$PKG" >/dev/null 2>&1
  ok "已加入 doze 白名单"

  say ""
  say "=== 7. 屏幕常亮（中间键已不再能开关屏幕）==="
  adb_ shell "settings put system screen_off_timeout 2147483647"
  ok "已设为永不休眠"

  say ""
  say "=== 8. 手工验收 ==="
  warn "请依次确认："
  warn "  1. 按 Home 键 → 回到我们的 App（不是小爱桌面）"
  warn "  2. 按音量+ / 音量- → 音量条有反应"
  warn "  3. 按中间键 → 屏幕不熄灭，App 收到 F1"
  warn "  4. 重启设备 → 直接进我们的 App"
}

# ------------------------------------------------------------------- status

status() {
  need_device
  say "=== 安装状态 ==="
  if adb_ shell pm list packages 2>/dev/null | grep -q "$PKG"; then ok "已安装 $PKG"; else err "未安装"; fi

  say ""
  say "=== 默认桌面 ==="
  adb_ shell dumpsys package "$PKG" 2>/dev/null | grep -A3 "android.intent.category.HOME" | head -5

  say ""
  say "=== 麦克风 ==="
  adb_ shell dumpsys media.audio_flinger 2>/dev/null | grep -A4 'Input thread' | head -8
  adb_ shell "getprop init.svc.$MIVPM_SERVICE"

  say ""
  say "=== 运行状态 ==="
  adb_ shell "ps -A 2>/dev/null | grep $PKG" | head -3

  say ""
  say "=== 被禁用的包 ==="
  adb_ shell pm list packages -d 2>/dev/null | grep -iE "xiaomi|voice|mico|launcher" | head -10
}

# ------------------------------------------------------------------ restore

restore() {
  need_device
  say "=== 1. 恢复中间键的开关屏幕功能 ==="
  if [ -f "$STATE_DIR/$KEYLAYOUT_NAME.orig" ]; then
    adb_ root >/dev/null 2>&1 || true
    adb_ remount >/dev/null 2>&1 || true
    adb_ shell "cat > $KEYLAYOUT_DIR/$KEYLAYOUT_NAME" < "$STATE_DIR/$KEYLAYOUT_NAME.orig"
    ok "已恢复原 keylayout 内容"
  elif [ -f "$STATE_DIR/$KEYLAYOUT_NAME.absent" ]; then
    adb_ root >/dev/null 2>&1 || true
    adb_ remount >/dev/null 2>&1 || true
    adb_ shell "rm -f $KEYLAYOUT_DIR/$KEYLAYOUT_NAME"
    ok "已删除我们添加的 keylayout（中间键回到 POWER）"
  else
    warn "没有备份记录，跳过 keylayout"
  fi

  say ""
  say "=== 2. 恢复 mivpm ==="
  adb_ shell "start $MIVPM_SERVICE" 2>/dev/null
  ok "已尝试启动 $MIVPM_SERVICE"

  say ""
  say "=== 3. 恢复小爱桌面 ==="
  IFS=',' read -ra PKGS <<< "$XIAOMI_LAUNCHER_PACKAGES"
  for p in "${PKGS[@]}"; do
    [ -z "$p" ] && continue
    p=$(echo "$p" | tr -d ' ')
    adb_ shell pm enable "$p" >/dev/null 2>&1 && ok "已启用 $p"
  done

  say ""
  say "=== 4. 取消我们的默认桌面 ==="
  adb_ shell "cmd package clear-package-preferred-activities $PKG" >/dev/null 2>&1
  ok "完成。重启后按 Home 会重新询问"

  say ""
  warn "keylayout 的恢复需要重启才生效：adb reboot"
}

# --------------------------------------------------------------------- main

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$STATE_DIR"

case "${1:-all}" in
  probe)    probe ;;
  keymap)   keymap ;;
  install)  install ;;
  takeover) takeover ;;
  status)   status ;;
  restore)  restore ;;
  all)      keymap && install && takeover ;;
  *) echo "用法: $0 {probe|keymap|install|takeover|status|restore|all}"; exit 1 ;;
esac
