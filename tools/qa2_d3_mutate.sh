#!/usr/bin/env bash
# QA 独立复核 —— D3反向验证：故意破坏修复，确认对应的冒烟用例会红。
#
# ⚠️ 关键方法论说明：工程师做了三层（①store.mark_pending 的 after_commit 钩子、
# ②版本戳 seqlock、③各调用点显式补失效）。这三层**互相替代**，
# 所以「删掉第③层，smoke_memory 的 I 段会不会红」这个问题本身是有歧义的 ——
# 如果 ① 在工作，③ 本来就是冗余的，删了当然还绿。
# 真正的反向验证必须**逐层单独打穿**，看每层各自被哪条用例守着。
#
# 每次改动前先备份，改完必���还原，最后校验文件与原始一致。
set -u
cd "$(dirname "$0")/.."
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
mkdir -p out/mut_backup

SERVER="server/app/memory"
declare -a FILES=("$SERVER/store.py" "$SERVER/router.py" "$SERVER/service.py" "$SERVER/textutil.py")
for f in "${FILES[@]}"; do
  cp "$f" "out/mut_backup/$(basename "$f")"
done

restore() {
  for f in "${FILES[@]}"; do
    cp "out/mut_backup/$(basename "$f")" "$f"
  done
  find server -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
}
trap restore EXIT

run_smoke() {
  local name="$1"
  "$PY" "tools/$name.py" > "out/mut_$1.txt" 2>&1
  local rc=$?
  local summary
  summary=$(grep -oE "[0-9]+/[0-9]+ passed" "out/mut_$1.txt" | tail -1)
  local fails
  fails=$(grep -c "\[FAIL\]" "out/mut_$1.txt" 2>/dev/null || echo 0)
  echo "    $name: exit=$rc  ${summary:-无汇总}  FAIL数=$fails"
}

verify_restored() {
  local ok=1
  for f in "${FILES[@]}"; do
    if ! diff -q "$f" "out/mut_backup/$(basename "$f")" >/dev/null; then
      echo "    ⚠️ 未还原: $f"; ok=0
    fi
  done
  [ $ok -eq 1 ] && echo "    ✅ 全部源文件已还原"
}

echo "================================================================"
echo "D3.0 基线（未改动）"
echo "================================================================"
run_smoke smoke_memory
cp out/mut_smoke_memory.txt out/mut_baseline_smoke_memory.txt
run_smoke qa_cache_race

################################################################
echo
echo "================================================================"
echo "M1: 只删 router.py:220 的显式补失效（保留 mark_pending钩子）"
echo "     目的：看 smoke_memory I 段到底守的是哪一层"
echo "================================================================"
sed -i '220s/^invalidate(user.id, agent_id)/pass  # MUTANT: 删掉显式补失效/' "$SERVER/router.py"
sed -n '218,222p' "$SERVER/router.py" | sed 's/^/    | /'
run_smoke smoke_memory
restore

################################################################
echo
echo "================================================================"
echo "M2: 删store.add_fact 里的 mark_pending + router 显式补失效"
echo "     （打穿第①层和第③层，只剩第②层版本戳）"
echo "================================================================"
sed -i 's/^        mark_pending(self.db, user_id, agent_id)$/        pass  # MUTANT/' "$SERVER/store.py"
sed -i '220s/^invalidate(user.id, agent_id)/pass  # MUTANT/' "$SERVER/router.py"
sed -i '53s/^    invalidate(user_id, agent_id)$/    pass  # MUTANT/' "$SERVER/service.py"
grep -n "MUTANT" "$SERVER/store.py" "$SERVER/router.py" "$SERVER/service.py" | sed 's/^/    /'
run_smoke smoke_memory
run_smoke qa_cache_race
restore

################################################################
echo
echo "================================================================"
echo "M3: 只打穿第②层（_cache_put 的版本比对改成永远放行）"
echo "     目的：看有没有用例守 seqlock"
echo "================================================================"
sed -i 's/if entry.version != _versions.get(key, 0):/if False:  # MUTANT 关掉版本比对/' "$SERVER/store.py"
grep -n "MUTANT" "$SERVER/store.py" | sed 's/^/    /'
run_smoke smoke_memory
run_smoke qa_cache_race
restore

################################################################
echo
echo "================================================================"
echo "M4: 打穿注入清洗（sanitize_for_prompt 原样返回）"
echo "================================================================"
sed -i 's/^    if not text:$/    return str(text or "")  # MUTANT 清洗失效\n    if not text:/' "$SERVER/textutil.py"
grep -n "MUTANT" "$SERVER/textutil.py" | sed 's/^/    /'
run_smoke smoke_memory
restore

################################################################
echo
echo "================================================================"
echo "M5: 打穿换行压缩（只保留控制字符清洗，不压换行）"
echo "================================================================"
sed -i "s|s = _NEWLINE_RE.sub(\" \", s)|s = s  # MUTANT 不压换行|" "$SERVER/textutil.py"
grep -n "MUTANT" "$SERVER/textutil.py" | sed 's/^/    /'
run_smoke smoke_memory
restore

################################################################
echo
echo "================================================================"
echo "还原校验"
echo "================================================================"
verify_restored
echo
echo "各次结果汇总见 out/mut_*.txt"