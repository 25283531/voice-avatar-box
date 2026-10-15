from pathlib import Path

p = Path(r"C:\Users\Administrator\AppData\Local\Temp\qa3_sandbox\tools\qa3_golden_harness.py")
lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
new_line = 'SANDBOX = Path(r"C:\\Users\\Administrator\\AppData\\Local\\Temp\\qa3_sandbox")\n'
hit = 0
for i, ln in enumerate(lines):
    if ln.startswith("SANDBOX = "):
        lines[i] = new_line
        hit += 1
p.write_text("".join(lines), encoding="utf-8")
print("replaced:", hit)
print([x for x in lines if x.startswith("SANDBOX")][0].strip())
