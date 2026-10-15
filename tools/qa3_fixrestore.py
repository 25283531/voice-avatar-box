from pathlib import Path

# 修正两处：PRISTINE 本身即 server 内容，src 不应再带 "server" 一层
p = Path(r"G:\workbuddy\语音助手\tools\qa3_final_probe.py")
s = p.read_text(encoding="utf-8")
s = s.replace('src = PRISTINE / "server" / rel', 'src = PRISTINE / rel')
p.write_text(s, encoding="utf-8")
print("probe 已修正:", 'src = PRISTINE / rel' in s)

h = Path(r"G:\workbuddy\语音助手\tools\qa3_golden_harness.py")
t = h.read_text(encoding="utf-8")
t = t.replace('shutil.copytree(SANDBOX / "server", PRISTINE / "server")',
              'shutil.copytree(SANDBOX / "server", PRISTINE)')
t = t.replace('shutil.copy2(PRISTINE / "server" / rel, p)',
              'shutil.copy2(PRISTINE / rel, p)')
t = t.replace('all(md5(p) == md5(PRISTINE / "server" / p.relative_to(SANDBOX / "server"))',
              'all(md5(p) == md5(PRISTINE / p.relative_to(SANDBOX / "server"))')
h.write_text(t, encoding="utf-8")
print("harness 已修正:", 'PRISTINE / rel' in t)
