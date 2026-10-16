#!/usr/bin/env bash
#逐个跑变异脚本并复核 md5（只读观察者）
#
# 用途
# ----
# 独立 TMPDIR 下逐个运行，确认它们仍能正常检出变异、exit 正确，
# 且**工作区红线资产全程不变**。
#
# ⚠️ 本脚本**自身不修改任何工作区文件**，只读 + 起子进程。
# ⚠️ 绝不使用通配 kill：每个脚本用独立 timeout，超时只杀自己的 PID。
#
# 用法：bash tools/run_mutation_suite.sh [脚本名...]
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="C:/Users/Administrator/.workbuddy/binaries/python/envs/elder/Scripts/python.exe"

# 受保护资产：跑完之后逐个比对 md5
ASSETS=(
  server/app/persona/catalog.py
  server/app/persona/guardrails.py
  server/app/persona/compiler.py
  server/evals/assertions.py
  server/evals/redline_baseline.json
  server/evals/suites/invariants.json
  server/evals/suites/xiaozhi.json
)

DEFAULT_LIST=(
  qa3_a1_probe qa3_a5_fixed qa3_eval_mutation qa3_friction
  qa3_golden_challenge qa3_golden_harness qa3_guard_dependency
  qa3_independence qa3_redline_all qa3_redline_semantic
  qa3_sync qa3_undocumented qa4_gate_challenge qa4_verify_new_gates
  qa7_redline_gate
)

if [ "$#" -gt 0 ]; then LIST=("$@"); else LIST=("${DEFAULT_LIST[@]}"); fi

SANDBOX_PARENT="${MUTATION_SANDBOX_ROOT:-$(mktemp -d -t qmut_suite_XXXXXX)}"
mkdir -p "$SANDBOX_PARENT"

fingerprint() {
  ( cd "$ROOT" && md5sum "${ASSETS[@]}" 2>/dev/null )
}

echo "======================================================================"
echo "变异脚本批量跑测（独立 TMPDIR = $SANDBOX_PARENT）"
echo "======================================================================"
BEFORE="$(fingerprint)"

PASSN=0; FAILN=0; DIRTYN=0
RESULTS=()

for name in "${LIST[@]}"; do
  script="tools/${name}.py"
  if [ ! -f "$ROOT/$script" ]; then
    printf '%-26s SKIP（文件不存在）\n' "$name"
    RESULTS+=("$name|MISSING|-")
    continue
  fi

  # 每个脚本一个**独立**的 TMPDIR：彼此不踩踏，也不碰系统临时目录
  tmp="$(mktemp -d -d "$SANDBOX_PARENT/${name}_XXXXXX")"
  before_one="$(fingerprint)"

  ( cd "$ROOT" && \
    MUTATION_SANDBOX_ROOT="$tmp" TMPDIR="$tmp" TEMP="$tmp" TMP="$tmp" \
    timeout 900 "$PY" "$script" >"$tmp/out.txt" 2>&1 )
  rc=$?

  after_one="$(fingerprint)"
  verdict="OK"
  if [ "$before_one" != "$after_one" ]; then
    verdict="**WORKSPACE-CHANGED**"
    DIRTYN=$((DIRTYN+1))
  fi

  # 抓脚本自己的结论行（passed / 结论 / VERDICT）
  summary="$(grep -aoE '[0-9]+/[0-9]+ passed|VERDICT: *[A-Za-z-]+|结论：[^|]{0,40}' \
             "$tmp/out.txt" 2>/dev/null | tail -2 | tr '\n' ' ')"
  [ -z "$summary" ] && summary="(无结论文本)"

  printf '%-26s exit=%-4s %-20s %s\n' "$name" "$rc" "$verdict" "$summary"
  RESULTS+=("$name|$rc|$verdict")
  [ "$verdict" = "OK" ] && PASSN=$((PASSN+1)) || FAILN=$((FAILN+1))
done

echo
echo "======================================================================"
echo "工作区红线资产 md5 复核"
echo "======================================================================"
AFTER="$(fingerprint)"
if [ "$BEFORE" = "$AFTER" ]; then
  echo "✅ 全程零变化 —— 副本化变异生效，工作区未被写入"
  echo "$AFTER" | sed 's/^/    /'
else
  echo "❌ 工作区发生变化（逐项diff）："
  diff <(echo "$BEFORE") <(echo "$AFTER") | sed 's/^/    /'
fi

echo
echo "======================================================================"
echo "汇总：干净 $PASSN / 异常 $FAILN / 工作区被写 $DIRTYN"
echo "======================================================================"
for r in "${RESULTS[@]}"; do echo "  $r"; done