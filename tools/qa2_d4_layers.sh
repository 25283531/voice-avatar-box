#!/usr/bin/env bash
# QA 独立复核 —— D3b：逐层打穿，每层都用**两套**用例验证
# （工程师的 smoke_memory + 工程师的 qa_cache_race vs 我写的 qa2_a / qa2_b）
#目的：区分「这层修复无效」与「这层修复有效但没有用例守着它」。
set -u
cd "$(dirname "$0")/.."
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"
mkdir -p out/mut_backup2
F1=server/app/memory/store.py
F2=server/app/memory/router.py
F3=server/app/memory/service.py
F4=server/app/memory/textutil.py
for f in $F1 $F2 $F3 $F4; do cp "$f" "out/mut_backup2/$(basename $f)"; done
restore() { for f in $F1 $F2 $F3 $F4; do cp "out/mut_backup2/$(basename $f)" "$f"; done
  find server -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; }
trap restore EXIT

run() {
  local tag="$1" script="$2"
  "$PY" "tools/$script.py" > "out/m4_${tag}_${script}.txt" 2>&1
  local rc=$?
  local sum; sum=$(grep -oE "[0-9]+/[0-9]+ passed" "out/m4_${tag}_${script}.txt" | tail -1)
  local nf; nf=$(grep -c "\[FAIL\]" "out/m4_${tag}_${script}.txt" 2>/dev/null)
  printf "    %-46s exit=%s  %-14s FAIL=%s\n" "$script" "$rc" "${sum:-无汇总}" "$nf"
}

banner() { echo; echo "------------------------------------------------------------"; echo "$1"; echo "------------------------------------------------------------"; }

banner "M3 只打穿第②层 seqlock：_cache_put 的版本比对永远放行"
sed -i 's/if entry.version != _versions.get(key, 0):/if False:  # MUTANT/' $F1
grep -n "MUTANT" $F1 | sed 's/^/    /'
echo "  [工程师的用例]"
run M3 smoke_memory
run M3 qa_cache_race
echo "  [我写的用例]"
run M3 qa2_a_cache
restore

banner "M3b 更狠：_cache_get 的版本校验也关掉（seqlock 完全不存在）"
sed -i 's/if entry.version != _versions.get(key, 0):/if False:  # MUTANT/' $F1
sed -i 's/^    if entry.version != version:$/    if False:  # MUTANT/' $F1
grep -n "MUTANT" $F1 | sed 's/^/    /'
echo "  [工程师的用例]"
run M3b smoke_memory
run M3b qa_cache_race
echo "  [我写的用例]"
run M3b qa2_a_cache
restore

banner "M5 打穿「换行压成空格」（只留控制字符清洗）"
sed -i 's|^    s = _NEWLINE_RE.sub(" ", s)$|    s = s  # MUTANT|' $F4
grep -n "MUTANT" $F4 | sed 's/^/    /'
echo "  [工程师的用例]"
run M5 smoke_memory
echo "  [我写的用例]"
run M5 qa2_b_inject
restore

banner "M6 只打穿「角色标记转义」（_ROLE_KV_RE 失效，换行压缩保留）"
sed -i 's|^    s = _ROLE_KV_RE.sub(lambda m: m.group(1) + "：", s)$|    s = s  # MUTANT|' $F4
grep -n "MUTANT" $F4 | sed 's/^/    /'
echo "  [工程师的用例]"
run M6 smoke_memory
echo "  [我写的用例]"
run M6 qa2_b_inject
restore

banner "M7 只打穿「标签删除」（_TAG_RE 失效）"
sed -i 's|^    s = _TAG_RE.sub(" ", s)$|    s = s  # MUTANT|' $F4
grep -n "MUTANT" $F4 | sed 's/^/    /'
echo "  [我写的用例]"
run M7 qa2_b_inject
restore

banner "M8 打穿 store层 supersede 的归属校验（风险#4）"
sed -i 's/if user_id is not None and int(row.user_id) != int(user_id):/if False:  # MUTANT/' $F1
grep -n "MUTANT" $F1 | sed 's/^/    /'
echo "  [工程师的用例]"
run M8 smoke_memory
restore

banner "还原校验"
for f in $F1 $F2 $F3 $F4; do
  diff -q "$f" "out/mut_backup2/$(basename $f)" >/dev/null && echo "    ✅ $f" || echo "    ⚠️ 未还原 $f"
done