"""QA 独立复核 —— 焦点 C：换embedding 模型后query 向量缓存是否真清。

要点：
  C1 create(kind=embedding) -> 清
  C2 update: embedding->embedding / embedding->llm / llm->embedding 三个方向
  C3 delete(embedding) -> 清；delete(asr) -> 不清
  C4 反向：改 ASR / TTS / LLM **不该**清（不能连坐）
  C5 漏网之鱼：还有哪些入口能改 embedding 配置而没挂钩子
（不启服务、不用 TestClient —— 本环境没装 httpx，直接调路由函数 + 真实 Session）
"""
from __future__ import annotations

import os
import subprocess  # noqa: S404
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
DB_PATH = Path(tempfile.gettempdir()) / "elder_qa2_c.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["ELDER_DB"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ELDER_JWT_SECRET"] = "qa2-c"

from app import models  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.memory import retrieve  # noqa: E402
from app.model_catalog import KIND_EMBEDDING, KIND_LLM_MAIN  # noqa: E402
from app.routers import providers as prov_mod  # noqa: E402

models.Base.metadata.create_all(engine)
results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def prime() -> None:
    retrieve.clear_query_cache()
    retrieve._query_cache["__sentinel__"] = (0.1, 0.2, 0.3)


def is_cleared() -> bool:
    return "__sentinel__" not in retrieve._query_cache


def main() -> int:
    s = SessionLocal()
    s.add(models.User(username="qa2c", password_hash="x", nickname="甲"))
    s.commit()
    uid = int(s.query(models.User).first().id)
    s.close()

    db = SessionLocal()
    user = db.get(models.User, uid)

    def pin(kind: str, model: str = "m1", **kw):
        d = {"kind": kind, "name": f"{kind}-cfg", "provider": "openai",
             "model": model, "api_key": "k", "base_url": "http://127.0.0.1:1/v1",
             "enabled": True, "priority": 1}
        d.update(kw)
        return prov_mod.ProviderIn(**d)

    def create(kind, model="m1", **kw):
        return int(prov_mod.create_provider(pin(kind, model, **kw), db, user).id)

    def update(pk, **kw):
        cur = db.get(models.AIProvider, pk)
        base = {"kind": cur.kind, "name": cur.name, "provider": cur.provider,
                "endpoint": cur.endpoint, "api_key": cur.api_key,
                "model": cur.model, "enabled": bool(cur.enabled),
                "priority": int(cur.priority or 1)}
        base.update(kw)
        return prov_mod.update_provider(pk, prov_mod.ProviderIn(**base), db, user)

    print(f"KIND_EMBEDDING={KIND_EMBEDDING!r} KIND_LLM_MAIN={KIND_LLM_MAIN!r}")

    print("\n── C1 create ──")
    prime()
    e1 = create(KIND_EMBEDDING)
    check("C1.1 create(kind=embedding) 清掉了 query 缓存", is_cleared(),
          f"cache={list(retrieve._query_cache)}")
    prime()
    a1 = create("asr")
    check("C1.2 create(kind=asr) **不该**清 query 缓存（不连坐）", not is_cleared(),
          f"cache={list(retrieve._query_cache)}")

    print("\n── C2 update（kind 可能被改） ──")
    prime()
    update(e1, model="m2")
    check("C2.1 embedding->embedding（只换 model 名）清了缓存", is_cleared(),
          f"cache={list(retrieve._query_cache)}")
    prime()
    update(e1, kind=KIND_LLM_MAIN)
    check("C2.2 embedding->llm（按**改前**的 kind）清了缓存", is_cleared(),
          f"cache={list(retrieve._query_cache)}")
    prime()
    update(e1, kind=KIND_EMBEDDING)
    check("C2.3 llm->embedding（按**改后**的 kind）清了缓存", is_cleared(),
          f"cache={list(retrieve._query_cache)}")
    prime()
    update(e1, enabled=False)
    check("C2.4 当前是 embedding，禁用它也清了缓存（合理：影响 embed 路由）", is_cleared())
    prime()
    update(a1, model="asr2")
    check("C2.5 改 ASR 配置不清 query 缓存（不连坐）", not is_cleared())
    prime()
    update(a1, base_url="http://127.0.0.1:2/v1", api_key="k2")
    check("C2.6 改 ASR 的 base_url/api_key 也不清（不连坐）", not is_cleared())

    print("\n── C3 delete ──")
    prime()
    prov_mod.delete_provider(a1, db, user)
    check("C3.1 delete(kind=asr) 不清 query 缓存（不连坐）", not is_cleared())
    prime()
    prov_mod.delete_provider(e1, db, user)
    check("C3.2 delete(kind=embedding) 清了 query 缓存", is_cleared(),
          f"cache={list(retrieve._query_cache)}")

    print("\n── C4 辅助函数直接单测 ──")
    for kind, expect_clear in [(KIND_EMBEDDING, True), ("asr", False), ("tts", False),
                               ("vad", False), (KIND_LLM_MAIN, False), ("rerank", False),
                               ("", False), (None, False)]:
        prime()
        prov_mod._clear_query_cache_if_embedding(kind)
        check(f"C4 · kind={kind!r} -> {'清' if expect_clear else '不清'}",
              is_cleared() == expect_clear)

    print("\n── C5 漏网之鱼 ──")
    import inspect  # noqa: PLC0415
    hook = "_clear_query_cache_if_embedding"
    # 从 FastAPI 路由表里取真正的写路由（比 dir() 反射可靠）
    from app.main import app as fastapi_app  # noqa: PLC0415

    def walk(routes, prefix=""):
        """新版本 FastAPI 用 _IncludedRouter 包了一层，要递归展开。"""
        for r in routes:
            orig = getattr(r, "original_router", None)
            if orig is not None:
                sub_prefix = prefix + (getattr(orig, "prefix", "") or "")
                yield from walk(getattr(orig, "routes", []), sub_prefix)
                continue
            sub = getattr(r, "routes", None)
            if sub is not None and not hasattr(r, "endpoint"):
                yield from walk(sub, prefix)
                continue
            path = prefix + (getattr(r, "path", "") or "")
            ep = getattr(r, "endpoint", None)
            if ep is not None or path:
                yield path, (getattr(r, "methods", None) or set()), ep

    write_routes = []
    for path, methods, ep in walk(fastapi_app.routes):
        m = methods & {"POST", "PATCH", "PUT", "DELETE"}
        if m and "provider" in path.lower():
            write_routes.append((path, sorted(m), ep))
    print(f"  providers 相关写路由: {[(p, m) for p, m, _ in write_routes]}")
    check("C5.0 确实找到了写路由（防空跑）", len(write_routes) >= 3, f"找到 {len(write_routes)} 条")
    missing = []
    for path, methods, ep in write_routes:
        if any(x in path for x in ("test", "export", "sync", "resolve", "safe", "catalog")):
            continue
        if ep is None or hook not in inspect.getsource(ep):
            missing.append((path, methods))
    check("C5.1 所有会改 provider 配置的写路由都挂了 query 缓存钩子",
          not missing, f"漏挂={missing}")

    print("\n  providers.py 之外的 AIProvider **写**操作（add/setattr/delete）：")
    out = subprocess.run(
        ["grep", "-rn", "-E", r"AIProvider\(|db\.add\(.*provider|provider\.[a-z_]+\s*=|"
         r"delete\(.*[Pp]rovider",
         "--include=*.py", str(ROOT / "server" / "app")],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    hits = [ln for ln in out.splitlines()
            if "providers.py" not in ln and "models.py" not in ln]
    for ln in hits:
        print(f"    {ln.strip()[:170]}")
    check("C5.2 providers.py 之外没有 AIProvider 的新建/改字段/删除（漏网入口）",
          not hits, f"命中 {len(hits)} 处：{hits}")

    print("\n  搜其它可能改模型路由/配置的路径：")
    out2 = subprocess.run(
        ["grep", "-rn", "-E", r"clear_query_cache|USAGE_EMBED|priority\s*=|\.enabled\s*=",
         "--include=*.py", str(ROOT / "server" / "app")],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    for ln in out2.splitlines():
        print(f"    {ln.strip()[:170]}")

    # 设备端/APP 侧有没有能改 embedding 选择的接口
    print("\n 搜 schema_patch / device_api 里的模型选择写入：")
    out3 = subprocess.run(
        ["grep", "-rn", "-i", "-E", "embed", "--include=*.py",
         str(ROOT / "server" / "app" / "routers"), str(ROOT / "server" / "app" / "schema_patch.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    for ln in out3.splitlines():
        if "providers.py" in ln:
            continue
        print(f"    {ln.strip()[:170]}")

    db.close()

    print()
    npass = sum(1 for _, ok, _ in results if ok)
    print("=" * 60)
    print(f"==== C 段：{npass}/{len(results)} passed ====")
    for n, ok, d in results:
        if not ok:
            print(f"  FAIL: {n}  {d}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())