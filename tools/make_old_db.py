"""造一个「改造前」的老库，用于验证 schema_patch 的升级路径。

老库特征（刻意复刻真实存量形态）：
  * ai_providers / avatars 缺本轮新增的全部列；
  * 有一条 kind='' 的历史角色行 —— 老库**没有 owner 字段**，
    所以无法判断它属于谁（这正是回填规则要处理的核心歧义）；
  * 有一条真实的平台 LLM 配置；
  * 形象上带着人格文本（人格的老家就在这里）；
  * agent_persona_profiles 里有**重复的 L1 全局画像行**（agent_id 为 NULL），
    而老的唯一约束不约束 NULL，所以这种行是可能存在的 ——
    建 agent_key 唯一索引前必须先清，否则 CREATE INDEX 直接失败、
    表现就是「升级后服务起不来」。

用法：python tools/make_old_db.py /tmp/old.db
"""
from __future__ import annotations

import os
import sqlite3
import sys

DDL = """
CREATE TABLE ai_providers (
  id INTEGER PRIMARY KEY, name VARCHAR(64), kind VARCHAR(16), provider VARCHAR(32),
  endpoint VARCHAR(512), api_key VARCHAR(512), model VARCHAR(128),
  params TEXT, priority INTEGER, enabled BOOLEAN, created_at DATETIME);
CREATE TABLE avatars (
  id INTEGER PRIMARY KEY, name VARCHAR(64), description VARCHAR(255),
  eaf_pack_name VARCHAR(128), eaf_pack_url VARCHAR(512), eaf_pack_sha256 VARCHAR(64),
  screen_width INTEGER, screen_height INTEGER, clips TEXT,
  system_prompt TEXT, tts_voice VARCHAR(64), emotion_lights TEXT,
  enabled BOOLEAN, created_at DATETIME);
CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64),
  password_hash VARCHAR(255), nickname VARCHAR(64), phone VARCHAR(32),
  is_admin BOOLEAN, created_at DATETIME);
CREATE TABLE agent_persona_profiles (
  id INTEGER PRIMARY KEY, user_id INTEGER, agent_id INTEGER,
  replies_count INTEGER, positive_count INTEGER, negative_count INTEGER,
  avg_reply_len FLOAT, avg_user_len FLOAT, humor_score FLOAT,
  address VARCHAR(32), address_counts TEXT, speed VARCHAR(16),
  catchphrases TEXT, manual TEXT, created_at DATETIME, updated_at DATETIME);
INSERT INTO ai_providers (id,name,kind,provider,enabled,priority,created_at)
  VALUES (1,'我妈的小暖','','',1,10,'2026-01-01');
INSERT INTO ai_providers (id,name,kind,provider,model,enabled,priority,created_at)
  VALUES (2,'主力对话','llm','deepseek','deepseek-chat',1,1,'2026-01-01');
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


def main() -> int:
    db = sys.argv[1] if len(sys.argv) > 1 else "old.db"
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print(f"老库已造：{db}")
    print("  * ai_providers 2 条（1 条 kind='' 历史角色 + 1 条 llm 配置）")
    print("  * avatars 1 条，带人格文本（人格的老家）")
    print("  * agent_persona_profiles 2 行重复的 L1 全局画像（agent_id IS NULL）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())