#!/usr/bin/env bash
# D5：只打穿第③层（各调用点显式补失效），保留第①层 mark_pending 钩子。
# 这一层的「显式补失效」到底有没有用例守着？
set -u
cd "$(dirname "$0")/.."
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
mkdir -p out/mut_backup3
F1=server/app/memory/store.py
F2=server/app/memory/router.py
F3=server/app/memory/service.py
for f in $F1 $F2 $F3; do cp "$f" "out/mut_backup3/$(basename $f)"; done
restore() { for f in $F1 $F2 $F3; do cp "out/mut_backup3/$(basename $f)" "$f"; done
  find server -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; }
trap restore EXIT

run() { local tag="$1" script="$2"
  "$PY" "tools/$script.py" > "out/m5_${tag}_${script}.txt" 2>&1
  local rc=$? sum nf
  sum=$(grep -oE "[0-9]+/[0-9]+ passed" "out/m5_${tag}_${script}.txt" | tail -1)
  nf=$(grep -c "\[FAIL\]" "out/m5_${tag}_${script}.txt" 2>/dev/null)
  printf "    %-30s exit=%s  %-14s FAIL=%s\n" "$script" "$rc" "${sum:-无汇总}" "$nf"; }

echo "=== M5-A: 只删 router.py:220（手动写入路径的显式补失效） ==="
sed -i '220s|^    invalidate(user\.id, agent_id)$|    pass  # MUTANT|' $F2
sed -n '219,221p' $F2 | sed 's/^/    | /'
run A smoke_memory
run A qa_cache_race
run A qa2_a_cache
restore

echo
echo "=== M5-B: 只删 service.py:53（_commit 的显式补失效） ==="
sed -i '53s|^    invalidate(user_id, agent_id)$|    pass  # MUTANT|' $F3
sed -n '52,54p' $F3 | sed 's/^/    | /'
run B smoke_memory
run B qa_cache_race
run B qa2_a_cache
restore

echo
echo "=== M5-C: 只删 router.py:425（删除路径的显式补失效） ==="
sed -i '425s|^    invalidate(row\.user_id, row\.agent_id)$|    pass  # MUTANT|' $F2
sed -n '424,426p' $F2 | sed 's/^/    | /'
run C smoke_memory
run C qa_cache_race
restore

echo
echo "=== M5-D: 删掉 store.mark_pending 里的 after_commit 钩子（第①层） ==="
sed -i 's|^        event.listen(db, "after_commit", _after_commit, once=True)$|        pass  # MUTANT|' $F1
grep -n 'MUTANT\|after_commit", _after' $F1 | sed 's/^/    /'
run D smoke_memory
run D qa_cache_race
run D qa2_a_cache
restore

echo
echo "=== M5-E: 只删 add_fact 里的「提交前 invalidate」 ==="
sed -i '221s|^        invalidate(user_id, agent_id)$|        pass  # MUTANT|' $F1
sed -n '220,223p' $F1 | sed 's/^/    | /'
run E smoke_memory
run E qa_cache_race
run E qa2_a_cache
restore

echo
echo "=== 还原校验 ==="
for f in $F1 $F2 $F3; do
  diff -q "$f" "out/mut_backup3/$(basename $f)" >/dev/null && echo "    ✅ $f" || echo "    ⚠️ 未还原 $f"
done