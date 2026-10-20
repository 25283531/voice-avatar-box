"""下载指定 run 的 firmware artifact 到 out/ci/fw<sha>/（带重试）。

坑：artifact 下载端点 302 到 SAS 签名对象存储，带 Authorization 会 401。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN = Path("G:/workbuddy/github-token.txt").read_text(encoding="utf-8").strip()
REPO = "25283531/voice-avatar-box"


def curl(args: str, tries: int = 6) -> str:
    last = ""
    for i in range(tries):
        r = subprocess.run(
            f'curl -s --noproxy "*" --max-time 120 {args}',
            shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        out = r.stdout or ""
        if r.returncode == 0 and out.strip():
            return out
        last = f"rc={r.returncode} out={out[:200]}"
        time.sleep(3 * (i + 1))
    print(f"!! curl 失败: {last}")
    return ""


def main() -> int:
    run_id, sha = sys.argv[1], sys.argv[2]
    dest = ROOT / "out" / "ci" / f"fw{sha}"
    dest.mkdir(parents=True, exist_ok=True)

    arts = []
    for _ in range(6):
        js = curl(f'-H "Authorization: Bearer {TOKEN}" '
                  f'"https://api.github.com/repos/{REPO}/actions/runs/{run_id}/artifacts"')
        try:
            arts = json.loads(js)["artifacts"]
            break
        except Exception:
            print(".. art 列表解析失败，重试")
            time.sleep(5)
    if not arts:
        print("!! 没有 artifact")
        return 3

    art = arts[0]
    print(f"artifact: {art['name']}  {art['size_in_bytes']:,} bytes  id={art['id']}")

    url = ""
    for _ in range(6):
        hdr = curl(f'-L --max-redirs 0 -D - -o /dev/null '
                   f'-H "Authorization: Bearer {TOKEN}" '
                   f'"https://api.github.com/repos/{REPO}/actions/artifacts/{art["id"]}/zip"')
        for line in hdr.splitlines():
            if line.lower().startswith("location:"):
                url = line.split(":", 1)[1].strip()
                break
        if url:
            break
        time.sleep(4)
    if not url:
        print("!! 没拿到重定向地址")
        return 4

    zp = dest / "_artifact.zip"
    ok = False
    for i in range(6):
        subprocess.run(f'curl -s --noproxy "*" --max-time 300 -o "{zp}" "{url}"', shell=True)
        if zp.exists() and zp.stat().st_size > 1_000_000:
            try:
                with zipfile.ZipFile(zp) as z:
                    z.extractall(dest)
                ok = True
                break
            except Exception as e:
                print(f".. 解压失败 {e}")
        time.sleep(3 * (i + 1))
    if not ok:
        print("!! 下载/解压失败")
        return 5
    zp.unlink()

    for f in sorted(dest.rglob("*")):
        if f.is_file():
            print(f"  {f.name:45s} {f.stat().st_size:>12,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
