#!/usr/bin/env bash
# 重复跑 smoke_models / smoke_roles，判定是「稳定失败」还是「偶发抖动」
SB=/c/Users/Administrator/AppData/Local/Temp/qa3_sandbox
PY=C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe
cd "$SB" || exit 9
for s in smoke_models smoke_roles; do
  echo "=========== $s ==========="
  for i in 1 2 3; do
    T=$(mktemp -d)
    TMPDIR="$T" TEMP="$T" TMP="$T" "$PY" "tools/$s.py" > "/tmp/rep_${s}_$i.txt" 2>&1
    rc=$?
    nf=$(grep -ac "\[FAIL\]" "/tmp/rep_${s}_$i.txt")
    echo "  第 $i 次: exit=$rc  [FAIL]数=$nf"
    grep -a "\[FAIL\]" "/tmp/rep_${s}_$i.txt" | head -3 | sed 's/^/      /'
  done
done
