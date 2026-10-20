/**
 * AI 配置弹窗「点遮罩关闭」的行为守卫。
 *
 * 为什么需要它：前端卫生门禁（smoke_frontend_hygiene）是静态检查，抓不到
 * 运行时鼠标行为导致的弹窗误关。本脚本在 Node 里跑 maskClose 的判定逻辑，
 * 模拟真实鼠标事件序列，把「选中字符串弹窗就消失」「按 Ctrl 弹窗就消失」
 * 这两个用户实测 bug 固化成断言。
 *
 * 用法：node tools/smoke_mask_close.js  （exit 0 = 全绿）
 */
const path = require('path');

// ---- 从 index.html 里抽出 maskClose 方法体，注入假 this 来跑 ----
const fs = require('fs');
const html = fs.readFileSync(path.join(__dirname, '..', 'server', 'app', 'static', 'index.html'), 'utf8');
const m = html.match(/maskClose\(which, e\)\{([\s\S]*?)\n    \},/);
if (!m) {
  console.error('[FAIL] 找不到 maskClose 方法 —— 方法被改名或删掉了？');
  process.exit(2);
}
const body = m[1];

// 用 Function 构造与 Vue methods 相同的运行环境（签名要带上 which, e）
const maskClose = new Function('which', 'e', `
  const state = this;
  ${body}
`);

function makeCtx() {
  return {
    maskDownAt: 0,
    pBox: true, aBox: true, pPreview: true,
    closePresetCalled: false,
    closePreset() { this.closePresetCalled = true; this.pEditBox = false; },
    pEditBox: true,
  };
}

// 模拟一次「按下→松开→click」的序列
function click(ctx, { ctrl = false, alt = false, shift = false, holdMs = 50, self = true } = {}) {
  if (self) ctx.maskDownAt = Date.now() - holdMs; // mousedown 落在遮罩自身
  const e = { ctrlKey: ctrl, altKey: alt, shiftKey: shift };
  maskClose.call(ctx, 'pBox', e);
}

const cases = [];
function check(name, fn) {
  const ctx = makeCtx();
  fn(ctx);
  const closed = ctx.pBox === null;
  cases.push({ name, closed });
  return closed;
}

// 场景 A：短点击遮罩（用户点空白处想关闭）→ 应该关
const a = check('短点击遮罩应关闭', c => click(c, { holdMs: 50 }));

// 场景 B：从遮罩拖进输入框选中文字（按下→拖 1.2s→松开）→ 不该关
const b = check('拖拽选中(>700ms)不应关闭', c => click(c, { holdMs: 1200 }));

// 场景 C：Ctrl+点击（用户按 Ctrl 复制粘贴时误点）→ 不该关
const c1 = check('Ctrl+点击不应关闭', c => click(c, { ctrl: true, holdMs: 50 }));

// 场景 D：Alt+点击（浏览器菜单快捷键）→ 不该关
const d = check('Alt+点击不应关闭', c => click(c, { alt: true, holdMs: 50 }));

// 场景 E：Shift+点击 → 不该关
const e = check('Shift+点击不应关闭', c => click(c, { shift: true, holdMs: 50 }));

// 场景 F：从弹窗内部拖出到遮罩松开（mousedown 不在遮罩上）→ 不该关
const f = check('从弹窗内拖出松开不应关闭', c => click(c, { holdMs: 50, self: false }));

// 场景 G：pEditBox 弹窗点遮罩 → 应走 closePreset（含额外清理）
const g = (() => {
  const ctx = makeCtx();
  ctx.maskDownAt = Date.now() - 50;
  maskClose.call(ctx, 'pEditBox', {});
  return ctx.closePresetCalled === true && ctx.pEditBox === false;
})();
cases.push({ name: 'pEditBox 点遮罩应走 closePreset', closed: g });

// ---- 反向变异：把修复改回「无守卫」形态，这些断言必须变红 ----
const mutatedBody = `
  if (which === 'pBox') this.pBox = null;
  else if (which === 'aBox') this.aBox = null;
  else if (which === 'pPreview') this.pPreview = null;
  else if (which === 'pEditBox') this.closePreset();
`;
const mutated = new Function('which', 'e', `const state = this; ${mutatedBody}`);
function mutClick(ctx, { ctrl = false, holdMs = 50, self = true } = {}) {
  if (self) ctx.maskDownAt = Date.now() - holdMs;
  mutated.call(ctx, 'pBox', { ctrlKey: ctrl });
}
const mutB = (() => { const c = makeCtx(); mutClick(c, { holdMs: 1200 }); return c.pBox === null; })();
const mutC = (() => { const c = makeCtx(); mutClick(c, { ctrl: true }); return c.pBox === null; })();
const mutCaught = mutB && mutC; // 变异后「拖拽/Ctrl」场景变成真的会关（bug 复现）

console.log('==== AI 配置弹窗遮罩关闭行为守卫 ====');
for (const { name, closed } of cases) {
  const wantClosed = /应关闭/.test(name) && !/不应/.test(name) || /应走/.test(name);
  const ok = closed === wantClosed;
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name} -> 实际${closed ? '关闭' : '保持'}（期望${wantClosed ? '关闭' : '保持'}）`);
}
const allOk = cases.every(({ name, closed }) => closed === (/应关闭/.test(name) && !/不应/.test(name) || /应走/.test(name)));

console.log('---- 反向变异（把守卫去掉，以下场景必须重现 bug）----');
console.log(`  [${mutB ? 'PASS' : 'FAIL'}] 变异后拖拽选中确实会误关（证明本测试有鉴别力）`);
console.log(`  [${mutC ? 'PASS' : 'FAIL'}] 变异后 Ctrl+点击确实会误关（证明本测试有鉴别力）`);
const mutOk = mutB && mutC;

if (allOk && mutOk) {
  console.log('\n==== 全绿：行为正确 + 测试有鉴别力 ====');
  process.exit(0);
} else {
  console.log('\n==== 有失败：行为不正确或测试失去鉴别力 ====');
  process.exit(1);
}
