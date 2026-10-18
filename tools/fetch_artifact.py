"""下载指定 CI run 的 artifact。

⚠️ 坑：artifact 下载端点会 **302 重定向**到对象存储（blob.core.windows.net）。
带 Authorization 头跟过去会被对象存储拒 401 —— 那个 URL 用的是 SAS 签名，
多带一个不认识的 Authorization 头反而破坏认证。所以必须：
  第一次请求带 auth 拿 Location，第二次请求**不带任何头**去取那个 URL。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

TOKEN = Path("G:/workbuddy/github-token.txt").read_text(encoding="utf-8").strip()
OUT = Path("G:/workbuddy/固件测试")


def sh(cmd: str) -> str:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8")
    return r.stdout


def main() -> int:
    run_id = sys.argv[1]
    js = sh(
        f'curl -s --noproxy "*" -H "Authorization: Bearer {TOKEN}" '
        f'"https://api.github.com/repos/25283531/voice-avatar-box/actions/runs/{run_id}/artifacts"'
    )
    arts = json.loads(js)["artifacts"]
    if not arts:
        print("!! 没有 artifact —— 用户要烧的文件不存在，这正是本项目踩过的假绿形态")
        return 3
    art = arts[0]
    print(f"artifact: {art['name']}  {art['size_in_bytes']:,} bytes")

    loc = sh(
        f'curl -s --noproxy "*" -L --max-redirs 0 -D - -o /dev/null '
        f'-H "Authorization: Bearer {TOKEN}" '
        f'"https://api.github.com/repos/25283531/voice-avatar-box/actions/artifacts/{art["id"]}/zip"'
    )
    url = ""
    for line in loc.splitlines():
        if line.lower().startswith("location:"):
            url = line.split(":", 1)[1].strip()
            break
    if not url:
        print("!! 没拿到重定向地址")
        return 4

    OUT.mkdir(parents=True, exist_ok=True)
    zp = OUT / "_artifact.zip"
    subprocess.run(f'curl -s --noproxy "*" -o "{zp}" "{url}"', shell=True, check=True)
    with zipfile.ZipFile(zp) as z:
        z.extractall(OUT)
    zp.unlink()

    merged = OUT / "elder-firmware-esp32s3-merged-flash.bin"
    app = OUT / "xiaozhi.bin"
    if not merged.exists():
        print("!! merged-flash.bin 不存在")
        return 5
    data = merged.read_bytes()
    print(f"\n=== 交付物 ===")
    print(f"elder-firmware-esp32s3-merged-flash.bin  {len(data):,} bytes")
    print(f"SHA256 : {hashlib.sha256(data).hexdigest()}")
    if app.exists():
        size = app.stat().st_size
        slot = 0x3F0000  # ota_0 = 4,128,768
        print(f"xiaozhi.bin : {size:,} bytes  (ota_0 槽 4,128,768 → 占 {size/slot*100:.2f}%，余 {(slot-size)//1024} KiB)")
        if size > slot:
            print("!! 超出 OTA 槽位")
            return 6
    else:
        print("xiaozhi.bin : 缺失")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
