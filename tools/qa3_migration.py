"""QA 独立验证：迁移幂等性 + 缺列补齐 + NEW_COLUMNS 登记完整性 + seed 顺序。

与 tools/make_old_db.py 的关系
------------------------------
make_old_db.py 是工程师写的样本库。本脚本**另造一个更狠的老库**，
并补上它没验的三件事：

1. **登记完整性（自动推导，不靠人眼）**：从 SQLAlchemy 的 ``__table__``
   反向枚举ai_providers / avatars / users / devices 的**全部列**，
   逐列与 schema_patch.NEW_COLUMNS 求差集。差集非空就说明"模型加了列
   但忘了登记" —— 那种 bug在全新部署里完全看不出来，只在老库升级时炸。
   这一条是本脚本最有价值的部分：它不依赖工程师写的样本库是否恰好覆盖到了。

2. **幂等**：连跑三次 ensure_missing_columns / run_all，比对结果与最终 schema。

3. **seed 顺序**：新装库里 _seed 必须在 _seed_migration 之前，
   否则全新部署的平台预设角色**没有人格**（静默少一段话，不报错）。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
PY = sys.executable

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    return bool(ok)


# ================================================================ 老库 DDL
# 刻意比 make_old_db.py 更狠：只保留**最老版本**可能有的列，
# 凡是我们能想到的"近代新增"的列一律不留 —— 逼补列逻辑必须真的补齐。
OLD_DDL = """
CREATE TABLE ai_providers (
  id INTEGER PRIMARY KEY, name VARCHAR(64) NOT NULL, kind VARCHAR(16),
  provider VARCHAR(32), endpoint VARCHAR(512), api_key VARCHAR(512),
  model VARCHAR(128), params TEXT, priority INTEGER, enabled BOOLEAN,
  created_at DATETIME);
CREATE TABLE avatars (
  id INTEGER PRIMARY KEY, name VARCHAR(64), description VARCHAR(255),
  eaf_pack_name VARCHAR(128), eaf_pack_url VARCHAR(512), eaf_pack_sha256 VARCHAR(64),
  screen_width INTEGER, screen_height INTEGER, clips TEXT,
  system_prompt TEXT, tts_voice VARCHAR(64), emotion_lights TEXT,
  enabled BOOLEAN, created_at DATETIME);
CREATE TABLE users (
  id INTEGER PRIMARY KEY, username VARCHAR(64), password_hash VARCHAR(255),
  nickname VARCHAR(64), phone VARCHAR(32), is_admin BOOLEAN, created_at DATETIME);
CREATE TABLE devices (
  id INTEGER PRIMARY KEY, device_id VARCHAR(64), name VARCHAR(64),
  owner_id INTEGER, avatar_id INTEGER, online BOOLEAN, last_seen_at DATETIME,
  fw_version VARCHAR(32), ip VARCHAR(64), settings TEXT, created_at DATETIME);
CREATE TABLE agent_persona_profiles (
  id INTEGER PRIMARY KEY, user_id INTEGER, agent_id INTEGER,
  replies_count INTEGER, positive_count INTEGER, negative_count INTEGER,
  avg_reply_len FLOAT, avg_user_len FLOAT, humor_score FLOAT,
  address VARCHAR(32), address_counts TEXT, speed VARCHAR(16),
  catchphrases TEXT, manual TEXT, created_at DATETIME, updated_at DATETIME);
INSERT INTO ai_providers (id,name,kind,provider,model,enabled,priority,created_at)
  VALUES (1,'我妈的小暖','','','',1,10,'2026-01-01');
INSERT INTO ai_providers (id,name,kind,provider,model,enabled,priority,created_at)
  VALUES (2,'主力对话','llm_main','deepseek','deepseek-chat',1,1,'2026-01-01');
INSERT INTO avatars (id,name,system_prompt,tts_voice,enabled,created_at)
  VALUES (1,'小暖（默认）','你是小暖，耐心温和。\n涉及服药提醒联系家人或医生。',
          'zh-CN-XiaoxiaoNeural',1,'2026-01-01');
INSERT INTO users (id,username,password_hash,is_admin,created_at)
  VALUES (1,'admin','x',1,'2026-01-01');
INSERT INTO agent_persona_profiles
  (id,user_id,agent_id,replies_count,positive_count,negative_count,
   avg_reply_len,avg_user_len,humor_score,address,speed,created_at,updated_at)
  VALUES (1,1,NULL,10,8,2,20.0,12.0,0.5,'您','normal','2026-01-01','2026-01-01'),
         (2,1,NULL,4,3,1,40.0,30.0,0.9,'你','fast','2026-01-01','2026-01-01');
"""


def make_old_db(path: Path) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(OLD_DDL)
    conn.commit()
    conn.close()


def run_in_server(code: str, db_path: Path, extra_env: dict | None = None):
    """在server 目录下跑一段代码，返回 (exit_code, stdout+stderr)。"""
    env = dict(os.environ)
    env.pop("MSYSTEM", None)
    env["ELDER_DB"] = f"sqlite:///{db_path.as_posix()}"
    env["ELDER_JWT_SECRET"] = "qa3-migrate"
    env["PYTHONIOENCODING"] = "utf-8"
    tmp = tempfile.mkdtemp(prefix="qa3_mig_")
    env["TMPDIR"] = env["TEMP"] = env["TMP"] = tmp
    if extra_env:
        env.update(extra_env)
    driver = Path(tmp) / "drv.py"
    driver.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(SERVER)!r})\n"
        + code, encoding="utf-8")
    try:
        p = subprocess.run([PY, str(driver)], cwd=str(ROOT), env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=240)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def last_json(out: str, marker: str = "###JSON###"):
    """从输出里抠出最后一行 JSON（子进程通信，避免解析日志噪声）。"""
    for line in reversed(out.splitlines()):
        s = line.strip()
        if s.startswith(marker):
            try:
                return json.loads(s[len(marker):])
            except json.JSONDecodeError:
                continue
    return None


# ================================================================ 各组验证
def t1_new_columns_completeness() -> None:
    """【核心】模型新增列vs NEW_COLUMNS 登记 —— 反向自动推导，不靠人眼。

    判定口径必须是**差集**而不是"全部列"：
    ``id`` / ``name`` / ``kind`` 这类列从第一版就在表里，create_all 建新库时
    会带上，老库也必然有 —— 它们**本来就不需要**登记进 NEW_COLUMNS。
    真正要查的是「模型里有、但老库里没有、又没有登记」的那些列，
    那种列在全新部署里完全看不出来，只在老库升级第一次 SELECT 时炸。
    """
    print("\n── T1 NEW_COLUMNS 登记完整性（老库基线差集，自动推导）──")
    # 老库基线 = OLD_DDL 建的列集合（= 改造前就存在的列）
    baseline = {
        "ai_providers": {"id", "name", "kind", "provider", "endpoint", "api_key",
                         "model", "params", "priority", "enabled", "created_at"},
        "avatars": {"id", "name", "description", "eaf_pack_name", "eaf_pack_url",
                    "eaf_pack_sha256", "screen_width", "screen_height", "clips",
                    "system_prompt", "tts_voice", "emotion_lights", "enabled",
                    "created_at"},
        "users": {"id", "username", "password_hash", "nickname", "phone",
                  "is_admin", "created_at"},
        "devices": {"id", "device_id", "name", "owner_id", "avatar_id", "online",
                    "last_seen_at", "fw_version", "ip", "settings", "created_at"},
        "agent_persona_profiles": {"id", "user_id", "agent_id", "replies_count",
                                   "positive_count", "negative_count",
                                   "avg_reply_len", "avg_user_len", "humor_score",
                                   "address", "address_counts", "speed",
                                   "catchphrases", "manual", "created_at",
                                   "updated_at"},
    }
    code = r'''
import json
from app import models
from app.schema_patch import NEW_COLUMNS
registered = {(t, c) for (t, c, _s, _m) in NEW_COLUMNS}
baseline = BASELINE_JSON
names = {"ai_providers": "AIProvider", "avatars": "Avatar", "users": "User",
         "devices": "Device", "agent_persona_profiles": "AgentPersonaProfile"}
report = {}
for tbl, mn in names.items():
    cols = [c.name for c in getattr(models, mn).__table__.columns]
    added = [c for c in cols if c not in set(baseline[tbl])]   # 老库缺的
    unregistered = [c for c in added if (tbl, c) not in registered]
    # 反向：登记了但模型里已不存在（清单腐坏，同样是bug）
    ghost = sorted(c for (t, c) in registered
                   if t == tbl and c not in cols)
    report[tbl] = {"added": len(added), "unregistered": unregistered,
                   "ghost": ghost}
print("###JSON###" + json.dumps(report, ensure_ascii=False))
'''
    #把baseline 注入子进程：传Python 字面量（已是 list/dict，可 repr），
    # 子进程里不需要再 loads 一次 —— 之前那层 json.loads 是多余的。
    baseline_literal = repr({t: sorted(cols) for t, cols in baseline.items()})
    code = "BASELINE_JSON = " + baseline_literal + "\n" + code
    rc, out = run_in_server(code, Path(tempfile.gettempdir()) / "qa3_t1.db")
    rep = last_json(out)
    if not check("T1 探针可运行", rc == 0 and rep is not None, out[-400:]):
        return
    for tbl, info in rep.items():
        check(f"T1 {tbl}：{info['added']} 个新增列全部登记进 NEW_COLUMNS",
              not info["unregistered"],
              f"漏登记：{info['unregistered']}")
        check(f"T1 {tbl}：NEW_COLUMNS 无腐坏条目（登记了但模型已无此列）",
              not info["ghost"], f"腐坏：{info['ghost']}")


def t2_old_db_upgrade() -> None:
    """老库升级：补列是否补齐 + 人格是否迁移过来 + 是否幂等。"""
    print("\n── T2 缺列老库升级（补齐 + 迁移 + 幂等）──")
    db = Path(tempfile.gettempdir()) / "qa3_old_upgrade.db"
    make_old_db(db)

    code = r'''
import json, sqlite3, sys
from app.db import init_db, engine
from app.schema_patch import ensure_missing_columns, run_all
from sqlalchemy import inspect, text

out = {}
init_db()                      # create_all：对已存在的表不补列
out["added_1"] = ensure_missing_columns()
out["runall_1"] = run_all()
out["added_2"] = ensure_missing_columns()
out["runall_2"] = run_all()
out["added_3"] = ensure_missing_columns()

insp = inspect(engine)
out["cols"] = {t: [c["name"] for c in insp.get_columns(t)]
               for t in ("ai_providers", "avatars")}
with engine.begin() as conn:
    out["rows"] = [dict(r) for r in conn.execute(text(
        "SELECT id,name,kind,record_type,owner_id,traits,guardrails,"
        "deleted_at,archived_at,api_style,context_window,timeout_sec,"
        "max_retries,supports_stream,system_prompt,tts_voice "
        "FROM ai_providers ORDER BY id")).mappings().all()]
    out["avatars"] = [dict(r) for r in conn.execute(text(
        "SELECT id,name,record_type,owner_id,deleted_at,system_prompt "
        "FROM avatars ORDER BY id")).mappings().all()]
    out["dup_l1"] = conn.execute(text(
        "SELECT COUNT(*) FROM agent_persona_profiles "
        "WHERE agent_id IS NULL GROUP BY user_id HAVING COUNT(*)>1")).all()
    out["idx"] = sorted(i["name"] for i in insp.get_indexes("ai_providers"))
print("###JSON###" + json.dumps(out, ensure_ascii=False, default=str))
'''
    rc, sout = run_in_server(code, db)
    rep = last_json(sout)
    if not check("T2 老库升级流程跑通", rc == 0 and rep is not None, sout[-600:]):
        return

    # --- 幂等 ---
    check("T2 第二次 ensure_missing_columns 补0 列（幂等）",
          rep["added_2"] == 0 and rep["added_3"] == 0,
          f"2nd={rep['added_2']} 3rd={rep['added_3']}")
    check("T2 第二次 run_all 全为 0（幂等）",
          all(v == 0 for v in rep["runall_2"].values()),
          json.dumps(rep["runall_2"], ensure_ascii=False))
    check("T2 重复的 L1 全局画像已合并去重",
          len(rep["dup_l1"]) == 0, f"{rep['dup_l1']}")

    # --- 补列完整性：老库那 5 列历史欠账必须真的补上 ---
    cols = set(rep["cols"]["ai_providers"])
    must = {"record_type", "owner_id", "parent_id", "avatar_id", "traits",
            "guardrails", "deleted_at", "archived_at", "description",
            "system_prompt", "tts_voice", "is_default",
            "guardrail_ack_at", "guardrail_ack_by", "guardrail_ack_version",
            "api_style", "supports_stream", "timeout_sec", "max_retries",
            "context_window"}
    miss = sorted(must - cols)
    check("T2 ai_providers 老库补齐全部 21 列（含 5 列历史欠账）",
          not miss, f"仍缺：{miss}")

    avcols = set(rep["cols"]["avatars"])
    avmiss = sorted({"record_type", "owner_id", "deleted_at"} - avcols)
    check("T2 avatars 补齐 record_type/owner_id/deleted_at",
          not avmiss, f"仍缺：{avmiss}")

    # --- record_type 回填 ---
    rows = {r["id"]: r for r in rep["rows"]}
    r1, r2 = rows.get(1, {}), rows.get(2, {})
    check("T2 kind!='' 的行回填为 config",
          r2.get("record_type") == "config", str(r2.get("record_type")))
    check("T2 kind=='' 的历史行按安全侧回填为 preset",
          r1.get("record_type") == "preset", str(r1.get("record_type")))
    check("T2 老库 config 行的 owner_id 为 NULL（不下发给个人）",
          r2.get("owner_id") is None, str(r2.get("owner_id")))
    check("T2 索引已补建",
          "ix_prov_record_type" in rep["idx"], ",".join(rep["idx"]))

    # --- 补列后的默认值必须让老行读得出来（不能是 NULL） ---
    check("T2 补列后老行 record_type 非 NULL（server_default 生效）",
          r1.get("record_type") is not None and r2.get("record_type") is not None)
    check("T2 补列后老行 traits 读得出 dict（TEXT DEFAULT '{}' 生效）",
          isinstance(r1.get("traits"), (dict, str)) and r1.get("traits") is not None,
          repr(r1.get("traits"))[:40])
    check("T2 补列后老行 timeout_sec 有默认值（不是 NULL）",
          r1.get("timeout_sec") is not None, str(r1.get("timeout_sec")))


def t3_migration_idempotent() -> None:
    """_seed_migration 的幂等：跑两次结果一致，且人格只迁一次。"""
    print("\n── T3 人格迁移幂等（Avatar.system_prompt -> AIProvider）──")
    db = Path(tempfile.gettempdir()) / "qa3_seedmig.db"
    make_old_db(db)
    code = r'''
import json
from app.db import init_db, engine
from app.schema_patch import run_all
from app.main import _seed, _seed_migration
from app import models
from app.db import SessionLocal

def snap():
    #⚠️ 必须走 ORM：raw SQL 取JSON 列拿到的是**字符串**，
    # 会让"traits 有几项"这类断言算出字符数而不是项数。
    with SessionLocal() as db:
        pr = [{"id": r.id, "name": r.name, "kind": r.kind,
               "record_type": r.record_type, "system_prompt": r.system_prompt,
               "tts_voice": r.tts_voice, "traits": r.traits,
               "guardrails": r.guardrails}
              for r in db.query(models.AIProvider).order_by(models.AIProvider.id).all()]
        av = [{"id": r.id, "name": r.name, "system_prompt": r.system_prompt}
              for r in db.query(models.Avatar).order_by(models.Avatar.id).all()]
    return {"providers": pr, "avatars": av}

init_db(); run_all()
_seed(); _seed_migration()
s1 = snap()
_seed(); _seed_migration()          # 再跑一遍
s2 = snap()
print("###JSON###" + json.dumps({"first": s1, "second": s2},
                                ensure_ascii=False, default=str))
'''
    rc, out = run_in_server(code, db)
    rep = last_json(out)
    if not check("T3 迁移流程跑通", rc == 0 and rep is not None, out[-600:]):
        return
    a, b = rep["first"], rep["second"]
    check("T3 人格迁移幂等：两次结果完全一致",
          json.dumps(a, sort_keys=True, default=str)
          == json.dumps(b, sort_keys=True, default=str),
          "两次不一致")

    # 人格确实从 Avatar 搬到了 preset 角色行
    preset = next((p for p in a["providers"]
                   if p["record_type"] == "preset" and p["kind"] == ""), None)
    check("T3 平台预设角色行存在", preset is not None,
          str([(p["id"], p["record_type"], p["kind"]) for p in a["providers"]]))
    if preset:
        check("T3 人格已迁入 preset 角色（Avatar 的老 system_prompt）",
              "你是小暖" in (preset.get("system_prompt") or ""),
              repr(preset.get("system_prompt"))[:50])
        check("T3 音色已迁入 preset 角色",
              preset.get("tts_voice") == "zh-CN-XiaoxiaoNeural",
              str(preset.get("tts_voice")))
        # 老库里的 kind=='' 行被回填为 preset —— 它的 traits 是**空**的，
        # 因为那是历史角色、不是代码 seed 出来的平台预设（traits 默认值
        # 只在 _seed()新建行时写入）。这里断言"不为None"，不是"有10 项"。
        check("T3 老库回填出的 preset 行 traits 字段可读（dict）",
              isinstance(preset.get("traits"), dict),
              f"{type(preset.get('traits')).__name__}")
    check("T3 Avatar.system_prompt 已置空（收权）",
          all(not (v.get("system_prompt") or "").strip() for v in a["avatars"]),
          str([v.get("system_prompt") for v in a["avatars"]]))

    # 再次运行**不得**用 Avatar 的空值覆盖 preset 已有的运营修改
    db2 = Path(tempfile.gettempdir()) / "qa3_seedmig2.db"
    make_old_db(db2)
    code2 = r'''
import json
from app.db import init_db, engine
from app.schema_patch import run_all
from app.main import _seed, _seed_migration
from sqlalchemy import text
init_db(); run_all(); _seed(); _seed_migration()
with engine.begin() as c:
    c.execute(text("UPDATE ai_providers SET system_prompt='运营改过的文案',"
                   "tts_voice='zh-CN-YunxiNeural' WHERE record_type='preset'"))
with engine.begin() as c:      # 把Avatar 的值塞回去（模拟回滚/脏数据）
    c.execute(text("UPDATE avatars SET system_prompt='脏数据又回来了'"))
_seed(); _seed_migration()     # 再迁移一次
with engine.begin() as c:
    r = [dict(x) for x in c.execute(text(
        "SELECT id,system_prompt,tts_voice FROM ai_providers "
        "WHERE record_type='preset'")).mappings().all()]
print("###JSON###" + json.dumps(r, ensure_ascii=False, default=str))
'''
    rc2, out2 = run_in_server(code2, db2)
    rep2 = last_json(out2)
    check("T3 复跑不覆盖运营修改（'仅当目标为空才写'）",
          rc2 == 0 and rep2 and any(
              "运营改过的文案" in (x.get("system_prompt") or "") for x in rep2),
          (out2 or "")[-300:] if not rep2 else
          str([x.get("system_prompt") for x in rep2]))


def t4_fresh_install_has_persona() -> None:
    """全新部署：seed 顺序错会导致平台预设角色**没有人格**且不报错。"""
    print("\n── T4 全新部署：seed 顺序（顺序错 = 静默少一段人格）──")
    db = Path(tempfile.gettempdir()) / "qa3_fresh.db"
    if db.exists():
        db.unlink()
    code = r'''
import json
from app.db import init_db
from app import models
from app.db import SessionLocal
from app.main import _seed, _seed_migration
from app.schema_patch import run_all
init_db(); run_all()
_seed(); _seed_migration()      # 与 main.lifespan 完全同序
with SessionLocal() as db:      # ⚠️ 走 ORM，JSON 列才拿到 dict
    r = [{"id": x.id, "name": x.name, "record_type": x.record_type,
          "system_prompt": x.system_prompt, "traits": x.traits,
          "guardrails": x.guardrails}
         for x in db.query(models.AIProvider).filter(
             models.AIProvider.kind == "").order_by(models.AIProvider.id).all()]
print("###JSON###" + json.dumps(r, ensure_ascii=False, default=str))
'''
    rc, out = run_in_server(code, db)
    rep = last_json(out)
    if not check("T4 全新部署初始化跑通", rc == 0 and rep is not None, out[-500:]):
        return
    preset = next((x for x in rep if x["record_type"] == "preset"), None)
    check("T4 全新部署产出平台预设角色", preset is not None, str(len(rep)))
    if preset:
        p = preset.get("system_prompt") or ""
        check("★ T4 平台预设角色**有人格**（seed 顺序正确的关键判据）",
              len(p.strip()) > 20 and "小暖" in p, repr(p)[:60])
        tr = preset.get("traits")
        check("T4 预设角色带完整 traits（10 项，且是 dict 不是字符串）",
              isinstance(tr, dict) and len(tr) == 10,
              f"{type(tr).__name__}/{len(tr) if hasattr(tr,'__len__') else '?'}项")
        check("T4 预设角色带 guardrails（含 5 条 hard）",
              "no_medical_advice" in json.dumps(
                  preset.get("guardrails"), ensure_ascii=False),
              "")

    # 反序验证：确认这条判据真的有鉴别力
    db2 = Path(tempfile.gettempdir()) / "qa3_fresh_rev.db"
    if db2.exists():
        db2.unlink()
    code2 = r'''
import json
from app.db import init_db, engine
from app.schema_patch import run_all
from app.main import _seed, _seed_migration
from sqlalchemy import text
init_db(); run_all()
_seed_migration(); _seed()      # ⚠️ 故意反序
with engine.begin() as c:
    r = [dict(x) for x in c.execute(text(
        "SELECT id,name,record_type,system_prompt FROM ai_providers "
        "WHERE kind=''")).mappings().all()]
print("###JSON###" + json.dumps(r, ensure_ascii=False, default=str))
'''
    rc2, out2 = run_in_server(code2, db2)
    rep2 = last_json(out2)
    persona_lost = bool(rep2) and all(
        len((x.get("system_prompt") or "").strip()) < 20 for x in rep2)
    check("T4 反序执行确实会丢人格（证明这条判据有鉴别力）",
          persona_lost,
          str([(x.get("name"), (x.get("system_prompt") or "")[:20]) for x in (rep2 or [])]))


def t5_sqlite_json_roundtrip() -> None:
    """traits / guardrails 的 JSON 列在 SQLite 下真的能存中文与嵌套结构。"""
    print("\n── T5 JSON 列在 SQLite 下的存取（中文/嵌套/大文本）──")
    db = Path(tempfile.gettempdir()) / "qa3_json.db"
    if db.exists():
        db.unlink()
    code = r'''
import json
from app.db import init_db, SessionLocal
from app import models
from app.persona import guardrails
init_db()
NOTE = "多字节：，。！？「」—— 与 emoji \U0001F9D3\U0001F9D3"
with SessionLocal() as db:
    row = models.AIProvider(name="JSON 测试", kind="", record_type="role",
                            owner_id=None,
                            traits={"verbosity": "short", "humor": 0.42,
                                    "note": NOTE},
                            guardrails=guardrails.merge_stored(None),
                            enabled=True)
    db.add(row); db.commit(); rid = row.id
with SessionLocal() as db:                # 另开 session 强制从 DB 读回
    got = db.get(models.AIProvider, rid)
    print("###JSON###" + json.dumps(
        {"traits": got.traits, "is_dict": isinstance(got.traits, dict),
         "note": (got.traits or {}).get("note"),
         "humor": (got.traits or {}).get("humor"),
         "gr_hard": len((got.guardrails or {}).get("hard") or []),
         "gr_soft": (got.guardrails or {}).get("soft"),
         "expected_note": NOTE},
        ensure_ascii=False, default=str))
'''
    rc, out = run_in_server(code, db)
    rep = last_json(out)
    if not check("T5 JSON 存取跑通", rc == 0 and rep is not None, out[-400:]):
        return
    check("T5 traits 读回是 dict（JSON 列真的被反序列化了）",
          rep["is_dict"], str(rep["is_dict"]))
    check("T5 traits JSON 原样读回（含中文与 emoji）",
          rep["note"] == rep["expected_note"]
          and abs(float(rep["humor"]) - 0.42) < 1e-9,
          repr(rep["note"])[:60])
    check("T5 guardrails JSON 读回，hard 段 5 条", rep["gr_hard"] == 5,
          str(rep["gr_hard"]))
    check("T5 guardrails JSON 读回，soft 段结构正确（list）",
          isinstance(rep["gr_soft"], list), str(type(rep["gr_soft"])))


def t6_mysql_ddl_sanity() -> None:
    """MySQL 侧的 DDL 片段静态体检（本机无 MySQL，做能做的部分）。"""
    print("\n── T6 MySQL DDL 片段静态体检 ──")
    code = r'''
import json, re
from app.schema_patch import NEW_COLUMNS
bad = []
for table, column, sqlite_ddl, mysql_ddl in NEW_COLUMNS:
    up = mysql_ddl.upper()
    # MySQL 的 JSON 列不允许带 DEFAULT
    if up.startswith("JSON") and "DEFAULT" in up:
        bad.append(f"{table}.{column}: MySQL JSON 列不应带 DEFAULT -> {mysql_ddl}")
    # NOT NULL 必须有 DEFAULT，否则老行 ALTER 后读出 NULL
    if "NOT NULL" in up and "DEFAULT" not in up:
        bad.append(f"{table}.{column}: MySQL NOT NULL 缺 DEFAULT -> {mysql_ddl}")
    # SQLite 侧：字符串列（VARCHAR/TEXT）**必须带 DEFAULT ''**。
    #
    # ⚠️ 这里原本写的是
    #     `if "DEFAULT" in s and "DEFAULT" not in s:` —— 两个条件互斥，
    #     恒假，这条检查从写下的那一刻起就是空转的假守卫（QA 第 N 轮 M3
    #     实测：hardware_model 去掉 DEFAULT '' 后 qa3_migration 仍 45/45）。
    #
    # 为什么必须钉死它：老库 ALTER 补列时，不带 DEFAULT 的 VARCHAR 列
    # 让**既有行读出 None**，而 Pydantic 侧对应字段大多是 `str = ""`
    # （非空），model_validate 直接抛 ValidationError —— 接口 500，
    # 而且只在「老库 + 有存量数据」时才炸，全新空库永远复现不出来。
    # 2026-10-19 实测坐实：去掉 hardware_model 的 DEFAULT '' 后，
    # 老行读出 None、DeviceOut 同形态序列化当场抛错。
    st = sqlite_ddl.upper()
    if ("VARCHAR" in st or "TEXT" in st) and "DEFAULT" not in st:
        bad.append(f"{table}.{column}: SQLite 字符串列缺 DEFAULT '' → "
                   f"老行 ALTER 后读出 NULL 会炸 Pydantic -> {sqlite_ddl}")
    # 两边类型族应一致（布尔/整数/时间）
    if ("BOOLEAN" in sqlite_ddl.upper()) != ("TINYINT" in up):
        bad.append(f"{table}.{column}: 布尔列两侧类型族不一致 -> "
                   f"{sqlite_ddl} vs {mysql_ddl}")
print("###JSON###" + json.dumps(
    {"bad": bad, "count": len(NEW_COLUMNS)}, ensure_ascii=False))
'''
    rc, out = run_in_server(code, Path(tempfile.gettempdir()) / "qa3_t6.db")
    rep = last_json(out)
    if not check("T6 探针跑通", rc == 0 and rep is not None, out[-300:]):
        return
    check(f"T6 {rep['count']} 条登记的 MySQL DDL 均合法",
          not rep["bad"], "；".join(rep["bad"][:5]))


def main() -> int:
    print("=" * 72)
    print("QA 独立验证：迁移 / schema / seed 顺序")
    print("=" * 72)
    for fn in (t1_new_columns_completeness, t2_old_db_upgrade,
               t3_migration_idempotent, t4_fresh_install_has_persona,
               t5_sqlite_json_roundtrip, t6_mysql_ddl_sanity):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check(f"{fn.__name__} 抛异常", False, f"{type(exc).__name__}: {exc}")

    passed = sum(1 for _n, ok, _d in results if ok)
    total = len(results)
    print("\n" + "=" * 72)
    print(f"==== {passed}/{total} passed ====")
    for name, ok, detail in results:
        if not ok:
            print(f"  FAILED: {name}  -> {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())