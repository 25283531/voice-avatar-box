#!/usr/bin/env python3
"""准备 LX04 离线唤醒词（sherpa-onnx KWS）运行所需的二进制资源。

## 为什么是"构建期下载"而不是把这些文件放进仓库

两套 `.so` + 一套中文 KWS 模型合计约 25 MB。二进制入库会让每次 clone
都多背 25 MB，而且以后升级版本会产生一串无法 diff 的大文件。这里改成
**CI 每次构建从官方产物下载并解包**，仓库里只留脚本与源码。

## 版本必须锁死（这不是"装最新版"）

`KWS_VERSION = 1.12.22` 与 `app/src/main/java/com/k2fsa/sherpa/onnx/` 里那
四份 Kotlin API 源码**同一个版本**。JNI 侧用 `GetFieldID` 按字段名读取
`KeywordSpotterConfig` / `OnlineModelConfig` 的属性，两个版本一旦错开，
症状是运行时 `UnsatisfiedLinkError` 或配置被静默读成 0 —— 编译全绿、单测全绿，
只有真机崩。所以升级要一起升，不能只改其中一处。

## 产物

```
lx04/app/src/main/jniLibs/armeabi-v7a/libonnxruntime.so
lx04/app/src/main/jniLibs/armeabi-v7a/libsherpa-onnx-jni.so
lx04/app/src/main/assets/kws/<modelDir>/encoder-epoch-12-...onnx
                              /decoder-epoch-12-...onnx
                              /joiner-epoch-12-...onnx
                              /tokens.txt
                              /keywords.txt   ← 我们自定义，只含「你好小智」
```

## 用法

```
python3 tools/fetch_kws_assets.py                # 下载 + 解包
python3 tools/fetch_kws_assets.py --apk <path>   # 用本地已缓存的 APK（离线/CI 缓存）
python3 tools/fetch_kws_assets.py --check        # 只校验产物是否齐备
```
"""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "lx04" / "app" / "src" / "main"

KWS_VERSION = "1.12.22"
ARCH = "armeabi-v7a"
MODEL_DIR = "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
APK_NAME = (
    f"sherpa-onnx-{KWS_VERSION}-{ARCH}-kws-zh-wenetspeech-zipformer.apk"
)
APK_URL = (
    "https://huggingface.co/csukuangfj/sherpa-onnx-apk/resolve/main/kws/"
    f"{KWS_VERSION}/{APK_NAME}"
)

JNI_DIR = APP / "jniLibs" / ARCH
ASSET_DIR = APP / "assets" / "kws" / MODEL_DIR

# 官方 APK 里带了两套 epoch 的模型（12 和 99），内容等价、各 13 MB。
# 官方 Demo 的 getKwsModelConfig(type=0) 用的是 epoch-12，我们照抄这一套。
MODEL_FILES = (
    "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
    "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
    "tokens.txt",
)

SO_FILES = ("libonnxruntime.so", "libsherpa-onnx-jni.so")

# 「你好小智」—— 与 ESP32-S3 固件的唤醒词逐字一致
# （firmware/xiaozhi/sdkconfig.defaults.esp32s3:28
#   CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y）。
#
# ⚠️ 格式是 sherpa-onnx 的**拼音 token** 序列，不是普通拼音、也不是汉字。
#    每个 token 必须在 tokens.txt 里存在（脚本会校验）：
#    n / ǐ / h / ǎo / x / iǎo / zh / ì
#    「zh」是整体声母 token，不能写成 z + h。
# ⚠️ 官方 keywords.txt 里没有「你好小智」（只有「你好军哥」「小爱同学」等），
#    所以这里必须自己写、不能用官方那份覆盖。
KEYWORD_LINE = "n ǐ h ǎo x iǎo zh ì @你好小智"
KEYWORDS_TEXT = KEYWORD_LINE + "\n"

CACHE = ROOT / "out" / "ci" / "kws"
CACHE_APK = CACHE / "kws-v7a.apk"


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    last = None
    for attempt in range(1, 5):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "elder-lx04/1.0"})
            with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as f:
                while True:
                    buf = r.read(1 << 20)
                    if not buf:
                        break
                    f.write(buf)
            if tmp.stat().st_size < 1_000_000:
                raise RuntimeError(f"下载内容过小：{tmp.stat().st_size} 字节")
            tmp.replace(dst)
            print(f"[ok] 下载 {dst.name} ({dst.stat().st_size / 1e6:.1f} MB)")
            return
        except Exception as exc:  # 网络间歇性 EOF，重试
            last = exc
            print(f"[retry {attempt}] {exc}")
            tmp.unlink(missing_ok=True)
    raise RuntimeError(f"下载失败：{last}")


def extract(apk: pathlib.Path) -> None:
    JNI_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(apk) as z:
        names = set(z.namelist())

        for so in SO_FILES:
            key = f"lib/{ARCH}/{so}"
            if key not in names:
                raise RuntimeError(f"APK 里缺少 {key}（版本/架构不匹配？）")
            (JNI_DIR / so).write_bytes(z.read(key))
            print(f"[ok] {so} {z.getinfo(key).file_size / 1e6:.1f} MB")

        for name in MODEL_FILES:
            key = f"assets/{MODEL_DIR}/{name}"
            if key not in names:
                raise RuntimeError(f"APK 里缺少 {key}")
            (ASSET_DIR / name).write_bytes(z.read(key))
            print(f"[ok] {name} {z.getinfo(key).file_size / 1e6:.1f} MB")

    # 校验关键词 token 确实都在 tokens.txt 里 —— 拼错一个 token 的后果是
    # 「唤醒词永远检不出」，而设备一切正常，最难在真机上定位。
    tokens = set()
    for line in (ASSET_DIR / "tokens.txt").read_text(encoding="utf-8").splitlines():
        parts = line.split(" ")
        if parts:
            tokens.add(parts[0])
    needed = KEYWORD_LINE.split(" @")[0].split(" ")
    missing = [t for t in needed if t and t not in tokens]
    if missing:
        raise RuntimeError(f"keywords.txt 里的 token 不在 tokens.txt 中：{missing}")

    (ASSET_DIR / "keywords.txt").write_text(KEYWORDS_TEXT, encoding="utf-8")
    print(f"[ok] 自定义唤醒词：{KEYWORD_LINE}")


def check() -> bool:
    missing = []
    for so in SO_FILES:
        if not (JNI_DIR / so).is_file() or (JNI_DIR / so).stat().st_size == 0:
            missing.append(str(JNI_DIR / so))
    for name in MODEL_FILES + ("keywords.txt",):
        if not (ASSET_DIR / name).is_file():
            missing.append(str(ASSET_DIR / name))
    if missing:
        print("[fail] 缺少资源：", *missing, sep="\n  ")
        return False
    kw = (ASSET_DIR / "keywords.txt").read_text(encoding="utf-8")
    if "你好小智" not in kw:
        print("[fail] keywords.txt 不是我们的唤醒词：", repr(kw))
        return False
    print("[ok] 唤醒资源齐备")
    for so in SO_FILES:
        print(f"  {so}  sha256={sha256(JNI_DIR / so)[:16]}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", type=pathlib.Path, help="用本地缓存的 APK，跳过下载")
    ap.add_argument("--check", action="store_true", help="只校验产物")
    args = ap.parse_args()

    if args.check:
        return 0 if check() else 1

    apk = args.apk or CACHE_APK
    if not apk.is_file():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"下载官方 KWS APK（{KWS_VERSION} / {ARCH}）…")
        download(APK_URL, apk)
    else:
        print(f"使用本地 APK 缓存：{apk} ({apk.stat().st_size / 1e6:.1f} MB)")

    extract(apk)
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main())
