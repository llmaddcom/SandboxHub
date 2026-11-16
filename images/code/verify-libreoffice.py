#!/usr/bin/env python3
"""构建期验证 headless LibreOffice（images/code/Dockerfile 5e 段调用，失败即构建失败，SandboxHub#101）。

数字人用 openpyxl 写的公式只有字符串、没有计算结果，读回核对看不出公式错。这里真实走一遍
agent 的用法：openpyxl 写公式 → `soffice --headless --convert-to xlsx` 重算 → openpyxl
data_only=True 读出数值；缓存过期的表也必须重算（见 libreoffice-recalc.xcd）；xlsx / docx 转 PDF。
"""
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import docx
import openpyxl

for c in ("soffice", "libreoffice"):
    if shutil.which(c) is None:
        sys.exit(f"缺系统命令: {c}")

tmp = Path(tempfile.mkdtemp())


def soffice(fmt: str, src: Path, out: Path) -> Path:
    subprocess.run(
        ["soffice", "--headless", "--convert-to", fmt, "--outdir", str(out), str(src)],
        check=True, capture_output=True, text=True, timeout=120,
    )
    dst = out / f"{src.stem}.{fmt}"
    assert dst.is_file() and dst.stat().st_size > 0, f"soffice 未产出 {dst}"
    return dst


def values(path: Path) -> dict[str, object]:
    ws = openpyxl.load_workbook(path, data_only=True).active
    return {k: ws[k].value for k in ("A3", "B1", "C1")}


# 1. openpyxl 写公式（无缓存值）→ soffice 重算 → data_only 读出数值
w = openpyxl.Workbook(); s = w.active
s["A1"] = 100; s["A2"] = -438060; s["A3"] = "=SUM(A1:A2)"; s["B1"] = "=A1*2"; s["C1"] = '="合计"&A3'
w.save(tmp / "t.xlsx")
assert values(tmp / "t.xlsx")["A3"] is None, "openpyxl 写出的公式不应带缓存值"
r1 = soffice("xlsx", tmp / "t.xlsx", tmp / "r1")
assert values(r1) == {"A3": -437960, "B1": 200, "C1": "合计-437960"}, f"重算结果不对: {values(r1)}"

# 2. 缓存过期：改 A1 的值、保留 B1 旧缓存 200 → 再转一遍必须得到 2000（默认「从不重算」会留 200）
with zipfile.ZipFile(r1) as z:
    parts = {n: z.read(n) for n in z.namelist()}
sheet = parts["xl/worksheets/sheet1.xml"].decode()
stale = sheet.replace("<v>100</v>", "<v>1000</v>", 1)
assert stale != sheet, "未找到 A1 的值"
parts["xl/worksheets/sheet1.xml"] = stale.encode()
(tmp / "stale").mkdir()
with zipfile.ZipFile(tmp / "stale" / "t.xlsx", "w", zipfile.ZIP_DEFLATED) as z:
    for n, b in parts.items():
        z.writestr(n, b)
assert values(tmp / "stale" / "t.xlsx")["B1"] == 200
r2 = soffice("xlsx", tmp / "stale" / "t.xlsx", tmp / "r2")
assert values(r2)["B1"] == 2000, f"缓存过期的表没有重算: {values(r2)}"

# 3. xlsx / docx 转 PDF
assert soffice("pdf", tmp / "t.xlsx", tmp / "pdf").read_bytes().startswith(b"%PDF")
d = docx.Document(); d.add_paragraph("沙盒 LibreOffice 验证"); d.save(tmp / "d.docx")
assert soffice("pdf", tmp / "d.docx", tmp / "pdf").read_bytes().startswith(b"%PDF")

shutil.rmtree(tmp, ignore_errors=True)
# 验证跑过的 soffice 会在 HOME 下留用户配置，不带进镜像
shutil.rmtree(Path.home() / ".config" / "libreoffice", ignore_errors=True)
print("libreoffice ok:", subprocess.run(["soffice", "--version"], capture_output=True, text=True).stdout.strip())
