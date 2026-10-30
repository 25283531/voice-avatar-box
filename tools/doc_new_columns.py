"""把 ``schema_patch.NEW_COLUMNS`` 的真实规模渲染进 ``docs/DEPLOY.md``，并校验二者一致。

## 为什么需要它

``docs/DEPLOY.md`` §8.3 曾经写死「当前 ``NEW_COLUMNS`` 登记了 **26 列**，
分布在 5 张表上」，而 2026-10-30 加完提醒软删除三列后实际是 **37 列 / 6 张表**。

这个偏差不是"文档不精确"这么轻：§8.3 是**升级前核对清单**的唯一依据。
按它去核对会得出"还缺 11 列"的结论，于是很可能有人手工去 ``ALTER TABLE``
补一批**早就存在**的列 —— 轻则白折腾，重则补错类型。

数字会继续变（每加一个模型字段都要登记一行），所以**不能靠人记得改**。
做法：把这段统计变成**生成块**，脚本负责渲染，CI 负责比对。

## 用法

    python tools/doc_new_columns.py            # 只打印，不改任何文件
    python tools/doc_new_columns.py --check    # 文档与代码不一致则退出码 1（CI 用）
    python tools/doc_new_columns.py --fix      # 就地重写文档里的统计块

## 判据

生成块的边界是两行 HTML 注释标记，``--check`` 做的是**逐字比对**，
不是"包含某个数字" —— 后者在表数、列数、表名任一变化时都可能漏报。
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRV = ROOT / "server"
DOC = ROOT / "docs" / "DEPLOY.md"

BEGIN = "<!-- NEW_COLUMNS:STATS:BEGIN（本块由 tools/doc_new_columns.py 生成，勿手改）-->"
END = "<!-- NEW_COLUMNS:STATS:END -->"


def load_columns() -> list[tuple]:
    """按服务端真实代码取 NEW_COLUMNS。

    必须 import 真实模块而不是解析源码文本：``NEW_COLUMNS`` 里有条件拼接
    （MySQL 与 SQLite 的 DDL 不同、注释掉的旧条目），正则数元组会数错。
    """
    sys.path.insert(0, str(SRV))
    os_chdir = None
    try:
        # ``app.config`` 在导入期就会打【数据库】【部署】那几行 INFO/WARNING。
        # 这个脚本只关心列数，那些日志会把 --check 的输出冲得看不清，
        # 在 CI 里还会让人以为"检查没跑"。导入前临时关掉。
        import logging
        _prev = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            from app import schema_patch  # noqa: WPS433（延迟导入：需要先改 sys.path）
            return list(schema_patch.NEW_COLUMNS)
        finally:
            logging.disable(_prev)
    finally:
        # 导入期可能会读 ELDER_DB 之类的环境变量；这里不改环境，只收拾 sys.path。
        if os_chdir is not None:
            pass


def render(columns: list[tuple]) -> str:
    per_table = Counter(c[0] for c in columns)
    # 保持 NEW_COLUMNS 里的首次出现顺序：与代码阅读顺序一致，diff 才稳定。
    order: list[str] = []
    for c in columns:
        if c[0] not in order:
            order.append(c[0])

    parts = "、".join(f"`{t}` {per_table[t]} 列" for t in order)
    lines = [
        BEGIN,
        f"当前 `NEW_COLUMNS` 登记了 **{len(columns)} 列**，"
        f"分布在 **{len(order)} 张表**上：{parts}。",
        "",
        "> 这段是**生成内容**：数字以 `server/app/schema_patch.py` 为准。"
        "改了 `NEW_COLUMNS` 就跑 `python tools/doc_new_columns.py --fix`，"
        "`server-ci` 会跑 `--check` 拦住忘改的情况。",
        END,
    ]
    return "\n".join(lines)


def split_doc(text: str) -> tuple[str, str, str] | None:
    i = text.find(BEGIN)
    j = text.find(END)
    if i < 0 or j < 0 or j < i:
        return None
    return text[:i], text[i:j + len(END)], text[j + len(END):]


def main(argv: list[str]) -> int:
    columns = load_columns()
    want = render(columns)

    if "--fix" in argv:
        text = DOC.read_text(encoding="utf-8")
        got = split_doc(text)
        if got is None:
            print(f"[FAIL] {DOC} 里找不到统计块标记，无法就地重写。")
            print("       请先手工把标记行贴进 §8.3，再跑 --fix。")
            return 2
        head, _old, tail = got
        DOC.write_text(head + want + tail, encoding="utf-8", newline="\n")
        print(f"[ OK ] 已重写 {DOC.relative_to(ROOT)} 的统计块（{len(columns)} 列 / "
              f"{len(set(c[0] for c in columns))} 张表）")
        return 0

    if "--check" in argv:
        text = DOC.read_text(encoding="utf-8")
        got = split_doc(text)
        if got is None:
            print(f"[FAIL] {DOC} 里找不到统计块标记。")
            print(f"       期望存在这两行：{BEGIN} / {END}")
            return 1
        _head, cur, _tail = got
        if cur.strip() != want.strip():
            print("[FAIL] docs/DEPLOY.md 的 NEW_COLUMNS 统计块与代码不一致")
            print(f"       代码：{len(columns)} 列 / {len(set(c[0] for c in columns))} 张表")
            print("       修法：python tools/doc_new_columns.py --fix")
            for ln in cur.strip().splitlines():
                if ln and not ln.startswith("<!--"):
                    print("       文档现在是：" + ln)
            return 1
        print(f"[ OK ] 文档统计块与代码一致（{len(columns)} 列 / "
              f"{len(set(c[0] for c in columns))} 张表）")
        return 0

    # 默认：只打印，方便人工核对
    per_table = Counter(c[0] for c in columns)
    order: list[str] = []
    for c in columns:
        if c[0] not in order:
            order.append(c[0])
    print(f"NEW_COLUMNS 共 {len(columns)} 列，{len(order)} 张表：")
    for t in order:
        cols = [c[1] for c in columns if c[0] == t]
        print(f"  {t:<26} {per_table[t]:>2} 列  {', '.join(cols)}")
    print()
    print("渲染后的文档块：")
    print(want)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
