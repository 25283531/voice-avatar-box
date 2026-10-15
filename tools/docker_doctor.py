#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""docker_doctor.py —— elder 服务栈一键诊断。

在**部署机**上运行（需要有 docker 命令）：

    python tools/docker_doctor.py            # 默认在 server/ 目录下找 compose 文件
    python tools/docker_doctor.py -C /path/to/server

它会依次收集并打印：
  1. docker / compose 版本
  2. compose 各服务状态
  3. elder-db 容器状态 + 最近日志里的错误行
  4. elder-server 容器状态 + 最近日志里的错误行
  5. 从临时容器实测到 elder-db 的 MySQL 连通性（带握手耗时）
  6. 数据卷信息

最后给出针对性的处置建议。**只读诊断，不修改任何数据。**
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ERR_PAT = re.compile(
    r"(error|fatal|failed|exception|cannot|refused|denied|lost connection|"
    r"OperationalError|Traceback|mysqld: |\[ERROR\])",
    re.I,
)


def run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """执行命令，返回 (returncode, 合并输出)。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"命令不存在: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"命令超时({timeout}s): {' '.join(cmd)}"


def section(title: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {title}")
    print("=" * 68)


def show(label: str, code: int, out: str, max_lines: int = 25, only_err: bool = False) -> None:
    print(f"\n$ {label}   (exit={code})")
    lines = out.splitlines()
    if only_err:
        picked = [ln for ln in lines if ERR_PAT.search(ln)]
        lines = picked or lines[-max_lines:]
    if len(lines) > max_lines:
        print(f"  ... 省略前 {len(lines) - max_lines} 行 ...")
        lines = lines[-max_lines:]
    for ln in lines:
        print("  " + ln)


def main() -> int:
    ap = argparse.ArgumentParser(description="elder 服务栈 Docker 诊断")
    ap.add_argument("-C", "--dir", default=None, help="含 docker-compose.yml 的目录")
    ap.add_argument("--tail", type=int, default=120, help="取日志的行数")
    args = ap.parse_args()

    workdir = Path(args.dir).resolve() if args.dir else Path.cwd()
    if not (workdir / "docker-compose.yml").is_file():
        # 允许在仓库根目录运行
        cand = workdir / "server" / "docker-compose.yml"
        if cand.is_file():
            workdir = cand.parent
        else:
            print(f"[!] 在 {workdir} 找不到 docker-compose.yml，请用 -C 指定目录")
            return 2
    print(f"工作目录: {workdir}")

    findings: list[str] = []

    # ---------------- 1. 版本 ----------------
    section("1. 环境版本")
    for label, cmd in (
        ("docker --version", ["docker", "--version"]),
        ("docker compose version", ["docker", "compose", "version"]),
    ):
        c, o = run(cmd)
        print(f"$ {label}: {o.strip() or '(无输出)'}")
        if c != 0:
            findings.append("docker 或 compose 不可用，请先确认 Docker Desktop / dockerd 已启动")
            print("\n[!] Docker 不可用，后续检查跳过。")
            return 1

    # ---------------- 2. 服务状态 ----------------
    section("2. compose 服务状态")
    c, o = run(["docker", "compose", "ps", "-a"], timeout=90)
    print(o.strip() or "(无输出)")

    c2, o2 = run(["docker", "ps", "-a", "--filter", "name=elder",
                  "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"])
    print("\n$ docker ps -a (elder*):")
    print(o2.strip() or "  (没有 elder 相关容器 —— 可能从未创建，或已被删除)")
    if "elder-db" not in o2:
        findings.append("elder-db 容器不存在：compose 未能创建，检查 compose 文件解析错误")

    # ---------------- 3. elder-db 日志 ----------------
    section("3. elder-db（MySQL）日志")
    c, o = run(["docker", "logs", "--tail", str(args.tail), "elder-db"], timeout=90)
    if c != 0:
        print(o.strip())
    else:
        show("docker logs elder-db（只显示错误相关行）", c, o, max_lines=40, only_err=True)
        if "already exist" in o or "Initializing database" in o:
            print("\n  [i] 日志显示数据目录初始化过程。首次启动较慢属正常。")

    c, o_inspect = run(["docker", "inspect", "elder-db", "--format",
                        "{{.State.Status}} restarts={{.RestartCount}} exitcode={{.State.ExitCode}}"], timeout=60)
    if c == 0:
        print(f"\n$ elder-db 状态: {o_inspect.strip()}")
        m = re.search(r"restarts=(\d+)", o_inspect)
        if m and int(m.group(1)) > 3:
            findings.append(
                f"elder-db 已重启 {m.group(1)} 次 —— mysqld 处于崩溃循环，"
                "通常意味着数据目录损坏或启动参数不被接受。"
                "处置：docker compose down（不要 -v，先保留数据）后看 mysqld 的第一段报错"
            )
        if "exitcode=1" in o_inspect:
            findings.append("elder-db 以 exitcode=1 退出 —— 看日志里第一条 mysqld 报错")

    # ---------------- 4. elder-server 日志 ----------------
    section("4. elder-server 日志")
    c, o = run(["docker", "logs", "--tail", str(args.tail), "elder-server"], timeout=90)
    if c != 0:
        print(o.strip())
    else:
        show("docker logs elder-server（只显示错误相关行）", c, o, max_lines=30, only_err=True)
        if "2013" in o or "Lost connection to MySQL" in o:
            findings.append(
                "elder-server 报 (2013, 'Lost connection to MySQL server during query') —— "
                "连上了 TCP 但读握手包时被断开，说明 mysqld 当时还不能处理查询"
                "（初始化中或正在崩溃重启）。本次代码已加入启动重试，"
                "若仍失败请优先解决 elder-db 自身的崩溃问题"
            )
        if "1045" in o or "Access denied" in o:
            findings.append("elder-server 报 1045 Access denied —— 密码与数据卷里已初始化的不一致。"
                            "若确认无数据可丢：docker compose down -v 后重建")

    # ---------------- 5. 实测连通性 ----------------
    section("5. 实测 elder-db 连通性")
    pw_c, pw_o = run(["docker", "exec", "elder-db", "printenv", "MYSQL_ROOT_PASSWORD"], timeout=60)
    pw = pw_o.strip() if pw_c == 0 else ""
    if not pw:
        print("无法取到 MYSQL_ROOT_PASSWORD（容器不存在或未运行），跳过实测。")
    else:
        cmd = ["docker", "exec", "elder-db", "sh", "-c",
               "mysql -h 127.0.0.1 -uroot -p\"$MYSQL_ROOT_PASSWORD\" "
               "-e 'SELECT VERSION() AS v, NOW() AS t' 2>&1"]
        c, o = run(cmd, timeout=90)
        print(f"$ 容器内自连 (exit={c}):")
        for ln in o.splitlines()[:10]:
            print("  " + ln)
        if c == 0:
            print("  [✓] mysqld 可正常查询")
        else:
            findings.append("容器内自连 MySQL 失败 —— 问题在 elder-db 自身，"
                            "与网络/编排无关，直接看 mysqld 报错")

        # 从 server 容器侧测（模拟后端视角）
        c2, o2 = run(["docker", "exec", "elder-server", "python", "-c",
                      "import socket,sys;p=3306;"
                      "s=socket.create_connection(('elder-db',p),timeout=5);"
                      "print('TCP elder-db:3306 OK');s.close()"], timeout=60)
        print(f"\n$ elder-server -> elder-db TCP (exit={c2}):")
        for ln in o2.splitlines()[:8]:
            print("  " + ln)
        if c2 != 0:
            findings.append("elder-server 到 elder-db 的 TCP 不通 —— 检查两者是否在同一 network")

    # ---------------- 6. 数据卷 ----------------
    section("6. 数据卷")
    for label, cmd in (
        ("docker volume ls (elder)", ["docker", "volume", "ls", "--filter", "name=elder"]),
        ("docker volume inspect", ["docker", "volume", "inspect", "server_elder-db-data"]),
    ):
        c, o = run(cmd, timeout=60)
        print(f"\n$ {label}:")
        for ln in (o.strip().splitlines() or ["(无)"]):
            print("  " + ln)

    # ---------------- 7. 结论 ----------------
    section("7. 诊断结论")
    if not findings:
        print("未发现明显异常。若服务仍起不来，把上面 3、4 两节的完整错误行贴出来。")
    else:
        for i, f in enumerate(findings, 1):
            print(f"{i}. {f}")

    print("\n常用处置（按代价从低到高）：")
    print("  a) 重启栈并观察：      docker compose up -d && docker compose logs -f elder-db")
    print("  b) 只重建后端：        docker compose up -d --force-recreate elder-server")
    print("  c) 清空数据重建(丢数据)：docker compose down -v && docker compose up -d")
    print("     —— 仅在确认库里没有需要保留的数据时执行；当前项目处于部署初期，"
          "若从未成功启动过，执行 c 通常是有效解。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
