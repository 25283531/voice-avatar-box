"""把本地提交通过 GitHub Git Data API 推送到远端（本机 HTTPS 到 github.com 被拦截时的唯一通路）。

用法：
    $env:GH_TOKEN="ghp_xxx"          (bash: export GH_TOKEN=ghp_xxx)
    python tools/push_github.py                 # 推送默认分支
    python tools/push_github.py --branch main   # 指定分支
    python tools/push_github.py --tag v0.1.0    # 顺带打标签

原理：blob -> tree -> commit -> PATCH/POST ref，复用本地 author/committer/时间戳，
      对象内容与本地完全一致（父提交被改写时 sha 会变，属正常）。
传输与重试统一走 tools/gh_api.py（curl + http1.1 + 节流 + 退避 + blob 断点续传）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gh_api import api, get_token  # noqa: E402

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_BASE = "/repos/25283531/voice-avatar-box"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
CACHE = os.path.join(tempfile.gettempdir(), "gh_push_blobs.json")
STATE = os.path.join(tempfile.gettempdir(), "gh_push_state.json")

# 改动路径 -> 需要触发的工作流（与各工作流里的 paths 过滤保持一致）
WORKFLOW_PATHS = {
    "server-ci.yml": ("server/", "tools/", ".github/workflows/server-ci.yml"),
    "docker-build.yml": ("server/", ".github/workflows/docker-build.yml"),
    "app-build.yml": ("app/", ".github/workflows/app-build.yml"),
}
WORKFLOW_INPUTS = {
    "server-ci.yml": None,
    "docker-build.yml": {"publish": False, "tag": "latest"},   # 自检，不推镜像
    "app-build.yml": {"platform": "all"},
}

get_token()


# ------------------------------------------------------------------ git 读取
def git(args, binary=False):
    r = subprocess.run(["git"] + args, cwd=REPO_DIR, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout if binary else r.stdout.decode("utf-8")


def ls_tree(ref: str):
    raw = git(["ls-tree", "-r", "-z", ref])
    out = []
    for part in raw.split("\0"):
        if not part:
            continue
        tab = part.index("\t")
        mode, typ, sha = part[:tab].split(" ")
        out.append({"mode": mode, "type": typ, "sha": sha, "path": part[tab + 1:]})
    return out


def parse_person(line: str):
    m = re.match(r"^(.*) <(.*)> (\d+) ([+-]\d{2})(\d{2})$", line)
    ts = int(m.group(3))
    sign = -1 if m.group(4)[0] == "-" else 1
    off = sign * (int(m.group(4)[1:]) * 60 + int(m.group(5)))
    d = dt.datetime.fromtimestamp(ts + off * 60, dt.UTC)
    return {"name": m.group(1), "email": m.group(2),
            "date": d.strftime("%Y-%m-%dT%H:%M:%S") + f"{m.group(4)}:{m.group(5)}"}


def read_commit(sha: str):
    raw = git(["cat-file", "commit", sha])
    idx = raw.index("\n\n")
    header, message = raw[:idx], raw[idx + 2:]
    return {
        "tree": re.search(r"^tree (.*)$", header, re.M).group(1),
        "parents": re.findall(r"^parent ([0-9a-f]{40})$", header, re.M),
        "author": parse_person(re.search(r"^author (.*)$", header, re.M).group(1)),
        "committer": parse_person(re.search(r"^committer (.*)$", header, re.M).group(1)),
        "message": message,
    }


# ------------------------------------------------------------------ blob 缓存
def load_cache() -> set:
    if os.path.exists(CACHE):
        try:
            return set(json.load(open(CACHE, encoding="utf-8")))
        except Exception:
            pass
    return set()


def save_cache(known: set) -> None:
    json.dump(sorted(known), open(CACHE, "w", encoding="utf-8"))


def load_state() -> dict:
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    json.dump(state, open(STATE, "w", encoding="utf-8"))


def pick_workflows(changed: list) -> dict:
    """按改动路径挑出需要触发的工作流（增量触发，避免每次全跑）。"""
    picked = {}
    for wf, prefixes in WORKFLOW_PATHS.items():
        if any(p.startswith(prefixes) for p in changed):
            picked[wf] = WORKFLOW_INPUTS[wf]
    return picked


# ------------------------------------------------------------------ 推送
def push_commit(sha: str, known: set, parent_override=None, use_cache=False):
    import base64
    c = read_commit(sha)
    entries = ls_tree(sha)
    print(f"\n[commit {sha[:7]}] {c['message'].splitlines()[0]}  ({len(entries)} 个文件)", flush=True)

    uniq = {e["sha"]: e for e in entries
            if e["type"] == "blob" and e["sha"] not in known and not (use_cache and e["sha"] in known)}
    print(f"  待上传 blob: {len(uniq)}（缓存命中 {sum(1 for e in entries if e['type']=='blob') - len(uniq)}）",
          flush=True)
    for i, (bsha, e) in enumerate(uniq.items(), 1):
        content = git(["cat-file", "blob", bsha], binary=True)
        r = api(f"{API_BASE}/git/blobs", "POST", {
            "content": base64.b64encode(content).decode("ascii"),
            "encoding": "base64"})
        if r.get("sha") != bsha:
            raise RuntimeError(f"blob sha 不一致: {e['path']}")
        known.add(bsha)
        if i % 20 == 0 or i == len(uniq):
            save_cache(known)
            print(f"    {i}/{len(uniq)}", flush=True)

    tree = api(f"{API_BASE}/git/trees", "POST", {"tree": [
        {"path": e["path"], "mode": e["mode"], "type": e["type"], "sha": e["sha"]}
        for e in entries]})
    if tree["sha"] != c["tree"]:
        raise RuntimeError(f"tree sha 不一致 {tree['sha']} != {c['tree']}")
    print(f"  tree   {tree['sha'][:7]} OK", flush=True)

    created = api(f"{API_BASE}/git/commits", "POST", {
        "message": c["message"], "tree": tree["sha"],
        "parents": [parent_override] if parent_override else c["parents"],
        "author": c["author"], "committer": c["committer"]})
    if created["sha"] != sha:
        if parent_override:
            print(f"  commit {created['sha'][:7]} OK（父提交已重写，本地 {sha[:7]}，内容一致）", flush=True)
        else:
            raise RuntimeError(f"commit sha 不一致 {created['sha']} != {sha}")
    else:
        print(f"  commit {created['sha'][:7]} OK", flush=True)
    return created["sha"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--no-cache", action="store_true", help="忽略已上传 blob 缓存，全量重传")
    # GitHub 的 push 事件本身就能按 paths 增量触发工作流，默认不再手动 dispatch
    ap.add_argument("--trigger", action="store_true",
                    help="推送后按改动路径手动 dispatch 受影响的工作流（默认不触发）")
    args = ap.parse_args()

    me = api("/user")
    print(f"认证身份: {me['login']}", flush=True)
    info = api(API_BASE)
    branch = args.branch or info.get("default_branch", "main")
    local = git(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    print(f"仓库 {info['full_name']}  远端 {branch}  本地 {local}", flush=True)

    ref = api(f"{API_BASE}/git/ref/heads/{branch}", allow_404=True)
    if ref is None:
        print("远端分支不存在（空仓库）→ 以空树为基准\n"
              "  注意：空仓库无法直接建 blob，会返回 409 Git Repository is empty；\n"
              "        请先用 contents API 建第一个提交（见 gh_api.py）。", flush=True)
        base, remote_sha = EMPTY_TREE, None
        commits = git(["rev-list", "--reverse", local]).split()
    else:
        remote_sha = ref["object"]["sha"]
        base = remote_sha
        print(f"远端 {branch} = {remote_sha[:7]}", flush=True)
        if base == git(["rev-parse", local]).strip():
            print("远端已是最新，无需推送")
            return
        try:
            commits = git(["rev-list", "--reverse", f"{base}..{local}"]).split()
        except RuntimeError:
            commits = []
        if not commits:
            print("本地与远端无共同祖先 → 整树推送，父提交指向远端 HEAD", flush=True)
            commits = git(["rev-list", "--reverse", local]).split()

    print(f"待推送提交: {len(commits)} -> {' -> '.join(s[:7] for s in commits)}", flush=True)

    known = load_cache() if not args.no_cache else set()
    if base != EMPTY_TREE and not known:
        try:
            known = {e["sha"] for e in ls_tree(base)}
        except RuntimeError:
            known = set()

    # 远端 HEAD 的树与本地某提交相同时（父提交重写会导致 sha 不同但内容一致），跳过该提交
    remote_tree = None
    if remote_sha:
        remote_tree = api(f"{API_BASE}/git/commits/{remote_sha}")["tree"]

    parent = remote_sha
    for sha in commits:
        if remote_tree and read_commit(sha)["tree"] == remote_tree:
            print(f"\n[skip {sha[:7]}] 远端 HEAD 已是同一棵树，跳过", flush=True)
            continue
        parent = push_commit(sha, known, parent)
    save_cache(known)

    if remote_sha:
        api(f"{API_BASE}/git/refs/heads/{branch}", "PATCH", {"sha": parent})
    else:
        api(f"{API_BASE}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": parent})
    after = api(f"{API_BASE}/git/ref/heads/{branch}")
    assert after["object"]["sha"] == parent, "ref 更新后校验失败"
    print(f"\n{branch} -> {parent[:7]}", flush=True)

    if args.tag:
        tagobj = api(f"{API_BASE}/git/tags", "POST", {
            "tag": args.tag, "message": f"release {args.tag}", "object": parent,
            "type": "commit", "tagger": read_commit(parent)["committer"]})
        api(f"{API_BASE}/git/refs", "POST", {"ref": f"refs/tags/{args.tag}", "sha": tagobj["sha"]})
        print(f"refs/tags/{args.tag} 已创建", flush=True)

    # ---------------------------------------------------------------- 增量触发
    # 通过 API 更新 ref 不会触发 push 事件，这里按「本次实际改动的文件」只触发受影响的工作流
    state = load_state()
    old = state.get("last_pushed_sha")
    try:
        changed = git(["diff", "--name-only", f"{old}..{local}"]).split() if old \
            else git(["ls-files"]).split()
    except RuntimeError:
        changed = git(["ls-files"]).split()
    state["last_pushed_sha"] = git(["rev-parse", local]).strip()
    save_state(state)

    picked = pick_workflows(changed)
    print(f"\n本次改动 {len(changed)} 个文件", flush=True)
    if not args.trigger:
        # 默认不 dispatch：GitHub 的 push 事件已按各工作流自己的 paths 做增量触发
        hit = "、".join(picked) if picked else "（未命中任何工作流，不会触发）"
        print(f"  GitHub 按 paths 自动增量触发 -> {hit}", flush=True)
        print("  如需手动触发用 --trigger", flush=True)
    elif not picked:
        print("  未命中任何工作流的 paths → 不触发", flush=True)
    else:
        print(f"  手动触发：{', '.join(picked)}", flush=True)
        for wf, inputs in picked.items():
            api(f"{API_BASE}/actions/workflows/{wf}/dispatches", "POST",
                {"ref": branch, **({"inputs": inputs} if inputs else {})})
            print(f"  ✓ {wf}", flush=True)

    print(f"\n完成：https://github.com/25283531/voice-avatar-box/tree/{branch}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
