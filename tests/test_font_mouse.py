# -*- coding: utf-8 -*-
"""回归测试：① 加密字体解码（真实抓取样本）② ghost-cursor 风格鼠标轨迹。"""
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
log = logging.getLogger("test")

from cxlab.font_decoder import FontDecoder, _name_to_char, extract_font_css  # noqa: E402
from cxlab.humanize import _bezier_points  # noqa: E402

# ---------------- 1) 加密字体解码（真实样本回归） ----------------
fixture_path = ROOT / "tests" / "fixtures" / "font_decode_cases.json"
fx = json.loads(fixture_path.read_text(encoding="utf-8"))
assert fx.get("font_css"), "fixture 缺少内嵌字体数据"
assert len(fx.get("cases", [])) >= 3, "用例太少"

html = ('<html><head><style id="cxSecretStyle">@font-face{font-family:"cxfont";'
        'src:url("data:application/font-ttf;charset=utf-8;base64,' + fx["font_css"] + '") format("truetype");}'
        '</style></head><body>x</body></html>')
assert extract_font_css(html), "未能从 HTML 提取字体 CSS"

decoder = FontDecoder.from_html(html, str(ROOT / "data" / "font_map_table.json"), log)
assert decoder is not None, "字体解码器构建失败"

for before, after in fx["cases"]:
    got = decoder.decode(before)
    assert got == after, f"解码不一致\n  期望: {after}\n  实际: {got}"

# 关键断言：混淆字确实被还原为正常汉字
assert decoder.decode("环糵保护孾") == "环境保护法"
assert decoder.decode("寁验寈") == "实验室"
print(f"[1] 加密字体解码回归 OK：{len(fx['cases'])} 条真实样本全部还原")

# ---------------- 2) 检测/工具函数 ----------------
assert FontDecoder.detect('<style id="cxSecretStyle">xx</style>') is True
assert FontDecoder.detect("<html><body>无字体</body></html>") is False
assert _name_to_char("uni4E00") == "一"
assert _name_to_char("A") == "A"
assert _name_to_char("notAGlyphName") is None
print("[2] detect/字形名解析 OK")

# ---------------- 3) ghost-cursor 风格鼠标轨迹 ----------------
pts = _bezier_points((0, 0), (600, 400))
assert len(pts) >= 8, f"轨迹点太少: {len(pts)}"
assert len(pts) <= 42, f"轨迹点过多: {len(pts)}"
assert abs(pts[-1][0] - 600) < 2 and abs(pts[-1][1] - 400) < 2, "终点未收敛到目标"
assert all(x >= 0 and y >= 0 for x, y in pts), "轨迹出现负坐标"

mid = pts[len(pts) // 2]
straight_y = 400 / 600 * mid[0]
curved = abs(mid[1] - straight_y) > 1.0
pts2 = _bezier_points((0, 0), (600, 400))
assert pts != pts2, "两次轨迹完全相同（缺少随机性）"
print(f"[3] 贝塞尔鼠标轨迹 OK：{len(pts)} 个点，中途偏离直线={curved}，具备随机性")

print("\n[OK] 字体解码 + 拟人化鼠标 回归测试全部通过")
