#!/usr/bin/env bash
# QA-3 D 项：全量回归。全部在沙箱里跑。
SB=/c/Users/Administrator/AppData/Local/Temp/qa3_sandbox
PY=C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe
cd "$SB" || exit 9

declare -a NAMES=(eval_persona smoke_server smoke_home smoke_quota
                 smoke_models smoke_memory smoke_roles qa3_edge
                 qa4_gate_challenge)
declare -a FILES=(eval_persona.py smoke_server.py smoke_home.py smoke_quota.py
                  smoke_models.py smoke_memory.py smoke_roles.py qa3_edge.py
                  qa4_gate_challenge.py)

echo "=================================================================="
printf "%-24s %-6s %s\n" "脚本" "exit" "结论"
echo "=================================================================="
PASSN=0; FAILN=0
for i in "${!NAMES[@]}"; do
  n="${NAMES[$i]}"; f="${FILES[$i]}"
  if [ ! -f "tools/$f" ]; then
    printf "%-24s %-6s %s\n" "$n" "SKIP" "（文件不存在）"
    continue
  fi
  T=$(mktemp -d)
  TMPDIR="$T" TEMP="$T" TMP="$T" "$PY" "tools/$f" > "/tmp/reg_$n.txt" 2>&1
  rc=$?
  last=$(grep -aoE "(PASS|FAIL|全部通过|全部 [0-9]+ 条断言通过|结论：[^\r\n]{0,60})" "/tmp/reg_$n.txt" | tail -1)
  [ -z "$last" ] && last=$(tail -2 "/tmp/reg_$n.txt" | tr -d '\r' | tr '\n' ' ' | cut -c1-60)
  if [ "$rc" -eq 0 ]; then PASSN=$((PASSN+1)); m="✅"; else FAILN=$((FAILN+1)); m="❌"; fi
  printf "%s %-22s %-6s %s\n" "$m" "$n" "$rc" "$last"
done
echo "=================================================================="
echo "通过 $PASSN / 失败 $FAILN"
echo "（smoke_mail 未跑：第 4 组会卡公网 SMTP 超时，主理人指示跳过）"
