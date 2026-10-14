"""GitHub REST API 小工具（本机专用）。

本机 github.com 的 HTTPS 被 SNI 拦截，只有 api.github.com 可用，且必须走 curl
（Python 自带 TLS 不稳定、HTTP/2 会被重置）→ 这里统一封一层带重试的 curl。

命令行用法：
    python tools/gh_api.py GET  /repos/25283531/voice-avatar-box
    python tools/gh_api.py POST /repos/25283531/voice-avatar-box/actions/workflows/app-build.yml/dispatches \
            '{"ref":"main","inputs":{"platform":"both"}}'
    python tools/gh_api.py runs                       # 最近 10 次工作流运行
    python tools/gh_api.py watch <run_id>             # 跟踪一次运行直到结束
    python tools/gh_api.py log  <run_id>              # 下载并打印失败作业日志

GH_TOKEN 从环境变量读取，或放 %TEMP%\\gh_token.txt。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time

OWNER = "25283531"
REPO = "voice-avatar-box"
API = f"/repos/{OWNER}/{REPO}"
_last_call = [0.0]
MIN_INTERVAL = 0.25          # 节流：本机到 api.github.com 密集请求会被重置


def get_token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    p = os.path.join(tempfile.gettempdir(), "gh_token.txt")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    raise SystemExit("未取到 GH_TOKEN（环境变量 GH_TOKEN 或 %TEMP%\\gh_token.txt）")


TOKEN = get_token()


def api(pathname: str, method: str = "GET", body=None, allow_404=False, retries: int = 6):
    # Git Bash 会把 argv 里以 / 开头的参数当成路径做 POSIX→Windows 转换，
    # 因此命令行上请用 "repos/xxx" 这种不带前导斜杠的写法（脚本内部补齐）。
    if pathname and not pathname.startswith("/") and not pathname.startswith("http"):
        pathname = "/" + pathname
    wait = MIN_INTERVAL - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    args = [
        "curl", "-sS", "--http1.1", "-m", "120", "-X", method,
        "-H", f"Authorization: Bearer {TOKEN}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-H", "User-Agent: voice-avatar-box",
        "-w", "\n%{http_code}",
        f"https://api.github.com{pathname}",
    ]
    tmp = None
    if body is not None:
        tmp = os.path.join(tempfile.gettempdir(), f"ghbody-{os.getpid()}-{int(time.time()*1000)}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        args += ["-H", "Content-Type: application/json", "--data-binary", f"@{tmp}"]
    last_err = ""
    try:
        for attempt in range(1, retries + 1):
            _last_call[0] = time.time()
            r = subprocess.run(args, capture_output=True)
            if r.returncode != 0:
                last_err = f"transport rc={r.returncode} {r.stderr.decode('utf-8', 'replace')[:120]}"
            else:
                raw = r.stdout.decode("utf-8", "replace")
                nl = raw.rfind("\n")
                text, code = raw[:nl], raw[nl + 1:].strip()
                # 204 / 空 body 是成功（如 workflow_dispatch）
                if code in ("200", "201", "204") and not text.strip():
                    return {"ok": True, "status": int(code)}
                try:
                    j = json.loads(text)
                except Exception:
                    last_err = f"non-json http={code} {text[:120]}"
                    j = None
                if j is not None:
                    if allow_404 and (code == "404" or
                                      (code == "409" and "empty" in str(j.get("message", "")).lower())):
                        return None
                    if code in ("200", "201", "204"):
                        return j
                    msg = str(j.get("message", ""))[:160]
                    last_err = f"http={code} {msg}"
                    if "rate limit" not in msg.lower():
                        raise RuntimeError(f"{method} {pathname}: http={code} {msg}")
            if attempt < retries:
                sleep = min(40, 3 * 2 ** (attempt - 1))
                print(f"    retry {attempt}/{retries}: {last_err} -> {sleep}s", flush=True)
                time.sleep(sleep)
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    raise RuntimeError(f"{method} {pathname} 连续 {retries} 次失败：{last_err}")


# ------------------------------------------------------------------ 子命令
def cmd_runs(_args):
    d = api(f"{API}/actions/runs?per_page=10")
    print(f"total: {d.get('total_count')}")
    for r in d.get("workflow_runs", []):
        print(f"{r['status']:<10} {str(r['conclusion']):<12} {r['name']:<16} {r['html_url']}")


def cmd_dispatch(args):
    body = {"ref": args[1] if len(args) > 1 else "main"}
    if len(args) > 2:
        body["inputs"] = json.loads(args[2])
    r = api(f"{API}/actions/workflows/{args[0]}/dispatches", "POST", body)
    print("dispatch ok" if r is not None else "dispatch done")


def cmd_watch(args):
    run_id = args[0]
    while True:
        r = api(f"{API}/actions/runs/{run_id}")
        print(f"{r['status']:<10} {str(r['conclusion']):<12} {r['name']}")
        if r["status"] == "completed":
            print(r["html_url"])
            return
        time.sleep(15)


def cmd_log(args):
    run_id = args[0]
    d = api(f"{API}/actions/runs/{run_id}/jobs?per_page=20")
    for j in d.get("jobs", []):
        if j["conclusion"] in (None, "success"):
            continue
        print(f"\n===== {j['name']}  ({j['conclusion']}) =====")
        # logs 接口返回 302 + text/plain，不能用 api()（会当成非 JSON 报错），单独用 curl -L 取
        out = subprocess.run([
            "curl", "-sSL", "--http1.1", "-m", "120",
            "-H", f"Authorization: Bearer {TOKEN}",
            "-H", "Accept: application/vnd.github+json",
            f"https://api.github.com{API}/actions/jobs/{j['id']}/logs"],
            capture_output=True).stdout.decode("utf-8", "replace")
        print(out)


def main():
    a = sys.argv[1:]
    if not a:
        raise SystemExit(__doc__)
    cmd, rest = a[0], a[1:]
    if cmd in ("GET", "POST", "PATCH", "PUT", "DELETE"):
        body = json.loads(rest[1]) if len(rest) > 1 else None
        print(json.dumps(api(rest[0], cmd, body), ensure_ascii=False, indent=2)[:4000])
    elif cmd == "runs":
        cmd_runs(rest)
    elif cmd == "dispatch":
        cmd_dispatch(rest)
    elif cmd == "watch":
        cmd_watch(rest)
    elif cmd == "log":
        cmd_log(rest)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
