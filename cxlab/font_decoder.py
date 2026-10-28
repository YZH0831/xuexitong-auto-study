# -*- coding: utf-8 -*-
"""学习通加密字体（cxSecretStyle）解码器。

原理
----
学习通对部分题目文本使用「自定义字体」做混淆：页面里内嵌一个 base64 TTF
（<style id="cxSecretStyle">），字体把常用汉字的码位映射到同形生僻字上。
真实字形轮廓不变，所以可以：

1. 解析内嵌字体，对每个字形计算「轮廓坐标的 MD5」(hash_glyph)；
2. 拿这个哈希去比对标准字形表 data/font_map_table.json（30884 条，由正常字体预生成）；
3. 命中即得到原始汉字 → 文字还原。

来源与许可
----------
本模块移植自开源项目（GPL-3.0）：
  - SocialSisterYi  xuexiaoyi-to-xuexitong-tampermonkey-proxy（算法原始出处）
  - Samueli924/chaoxing  api/cxsecret_font.py（本移植的参考实现）
参考用标准字形表 data/font_map_table.json 亦来自上述项目。
仅供个人学习使用，请勿对外分发（GPL-3.0 传染性条款）。

依赖：fontTools（纯 Python）。
"""
import base64
import hashlib
import json
import re
import unicodedata
from io import BytesIO
from pathlib import Path

# 康熙部首 → 对应汉字（还原后统一替换，避免出现部首字形）
KX_RADICALS_TAB = str.maketrans(
    "⼀⼁⼂⼃⼄⼅⼆⼇⼈⼉⼊⼋⼌⼍⼎⼏⼐⼑⼒⼓⼔⼕⼖⼗⼘⼙⼚⼛⼜⼝⼞⼟⼠⼡⼢⼣⼤⼥⼦⼧⼨⼩⼪⼫⼬⼭⼮⼯⼰⼱⼲⼳⼴⼵⼶⼷⼸⼹⼺⼻⼼⼽⼾⼿⽀⽁⽂⽃⽄⽅⽆⽇⽈⽉⽊⽋⽌⽍⽎⽏⽐⽑⽒⽓⽔⽕⽖⽗⽘⽙⽚⽛⽜⽝⽞⽟⽠⽡⽢⽣⽤⽥⽦⽧⽨⽩⽪⽫⽬⽭⽮⽯⽰⽱⽲⽳⽴⽵⽶⽷⽸⽹⽺⽻⽼⽽⽾⽿⾀⾁⾂⾃⾄⾅⾆⾇⾈⾉⾊⾋⾌⾍⾎⾏⾐⾑⾒⾓⾔⾕⾖⾗⾘⾙⾚⾛⾜⾝⾞⾟⾠⾡⾢⾣⾤⾥⾦⾧⾨⾩⾪⾫⾬⾭⾮⾯⾰⾱⾲⾳⾴⾵⾶⾷⾸⾹⾺⾻⾼髙⾽⾾⾿⿀⿁⿂⿃⿄⿅⿆⿇⿈⿉⿊⿋⿌⿍⿎⿏⿐⿑⿒⿓⿔⿕⺠⻬⻩⻢⻜⻅⺟⻓",
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶手支攴文斗斤方无日曰月木欠止歹殳毋比毛氏气水火爪父爻爿片牙牛犬玄玉瓜瓦甘生用田疋疒癶白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色艸虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉采里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍龜龠民齐黄马飞见母长",
)

# CJK 部首补充区（U+2E80~U+2EF3）→ 对应标准汉字。
# 这些部首字形与正字几乎一致（如 ⻰/龙、⻢/马），但码位不同，会让题库匹配失配。
# NFKC 对这区大多没有兼容分解，所以显式给出映射（每 2 个字符为一组：部首 + 正字）。
_CJK_RADICAL_PAIRS = (
    "⺁厂⺄乙⺈刀⺊卜⺌小⺍小⺎兀⺏尢⺐尣⺑尸⺒户⺓户⺔户⺕彐⺖心⺗心⺘手⺙攴"
    "⺛无⺜日⺝月⺞歹⺟母⺠民⺡气⺢水⺣火⺤爪⺥爪⺦父⺧牛⺨犬⺩玉⺪疋⺫目"
    "⺬示⺭示⺮竹⺯糸⺰糸⺱网⺲网⺳网⺴羊⺵羊⺶羊⺷羊⺸羊⺹老⺺耒⺻聿"
    "⺼肉⺽肉⺾艸⺿艸⻀艸⻁虎⻂衣⻃西⻄西⻅见⻆角⻇角⻈言⻉贝⻊足⻋车"
    "⻌辵⻍辵⻎辵⻏邑⻐邑⻑長⻒長⻓长⻔门⻕门⻖阜⻗阜⻘青⻙韋⻚页⻛风"
    "⻜飞⻝食⻞食⻟食⻠食⻡首⻢马⻣骨⻤鬼⻥鱼⻦鸟⻧卤⻨麦⻩黄⻪黾⻫齐"
    "⻬齐⻭齿⻮齿⻯龙⻰龙⻱龟⻲龟⻳龟"
)
CJK_RADICAL_TAB = str.maketrans({
    _CJK_RADICAL_PAIRS[i]: _CJK_RADICAL_PAIRS[i + 1]
    for i in range(0, len(_CJK_RADICAL_PAIRS) - 1, 2)
})

FONT_BASE64_PATTERN = re.compile(r"base64,([\w\W]+?)['\"]")
FONT_DATA_PREFIX = "data:application/font-ttf;charset=utf-8;base64,"
STYLE_ID = "cxSecretStyle"

_ref_cache = {"path": None, "hash2char": {}}


def load_reference_table(path, log=None):
    """加载标准字形表（字形哈希 → 字符），带缓存。"""
    path = str(path)
    if _ref_cache["path"] == path and _ref_cache["hash2char"]:
        return _ref_cache["hash2char"]
    hash2char = {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for char_name, glyph_hash in data.items():
            hash2char.setdefault(glyph_hash, char_name)
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log.warning(f"字体标准表加载失败（{path}）: {str(exc)[:80]}")
        data = {}
    _ref_cache["path"] = path
    _ref_cache["hash2char"] = hash2char
    if log is not None:
        log.info(f"字体标准表已加载：{len(data)} 条 / 可用哈希 {len(hash2char)} 个")
    return hash2char


def _name_to_char(name):
    """字形名 → 字符：支持 uniXXXX 与直接字符名（如 'A'）。"""
    if not name:
        return None
    if name.startswith("uni") and len(name) >= 7:
        try:
            return chr(int(name[3:7], 16))
        except ValueError:
            return None
    if len(name) == 1:
        return name
    return None


def hash_glyph(glyph):
    """字形轮廓坐标 + 标志位 的 MD5（与参考实现一致）。"""
    try:
        if glyph.numberOfContours <= 0:
            return ""
        pos_data = []
        last_index = 0
        for i in range(glyph.numberOfContours):
            end_point = glyph.endPtsOfContours[i]
            for j in range(last_index, end_point + 1):
                x, y = glyph.coordinates[j]
                flag = glyph.flags[j] & 0x01
                pos_data.append(f"{x}{y}{flag}")
            last_index = end_point + 1
        if not pos_data:
            return ""
        return hashlib.md5("".join(pos_data).encode()).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def font2map(font_data):
    """解析字体（路径/BytesIO/dataURL 字符串）→ {字形名: 轮廓哈希}。"""
    from fontTools.ttLib import TTFont

    if isinstance(font_data, str) and font_data.startswith(FONT_DATA_PREFIX):
        font_data = BytesIO(base64.b64decode(font_data[len(FONT_DATA_PREFIX):]))
    result = {}
    with TTFont(font_data, lazy=False) as font_file:
        table = font_file["glyf"]
        for name in table.glyphOrder:
            glyph_hash = hash_glyph(table.glyphs[name])
            if glyph_hash:
                result[name] = glyph_hash
    return result


def extract_font_css(html):
    """从页面 HTML 里取出内嵌字体的 base64 数据。"""
    if not html:
        return None
    m = re.search(rf'id=["\']{STYLE_ID}["\'][^>]*>(.*?)</style>', html, re.S | re.I)
    seg = m.group(1) if m else html
    m2 = FONT_BASE64_PATTERN.search(seg)
    return m2.group(1) if m2 else None


class FontDecoder:
    """加密字体解码器：detect → from_html → decode。"""

    def __init__(self, glyph_map, hash2char):
        self.glyph_map = glyph_map or {}
        self.hash2char = hash2char or {}

    @staticmethod
    def detect(html):
        """页面是否使用了加密字体。"""
        if not html:
            return False
        return ("cxSecretStyle" in html) or (FONT_DATA_PREFIX in html)

    @classmethod
    def from_html(cls, html, table_path, log=None):
        """从页面 HTML 构建解码器；失败返回 None。"""
        css_b64 = extract_font_css(html)
        if not css_b64:
            if log is not None:
                log.warning("页面中未找到内嵌加密字体，无法解码")
            return None
        hash2char = load_reference_table(table_path, log)
        if not hash2char:
            return None
        try:
            glyph_map = font2map(FONT_DATA_PREFIX + css_b64)
        except Exception as exc:  # noqa: BLE001
            if log is not None:
                log.warning(f"解析内嵌字体失败: {str(exc)[:80]}")
            return None
        if log is not None:
            log.info(f"加密字体已解析：{len(glyph_map)} 个字形")
        return cls(glyph_map, hash2char)

    def decode(self, text):
        """还原被混淆的文本；无法还原的字符原样保留。

        最后再做一次 Unicode NFKC 归一化：把 CJK 部首/兼容字形（如 ⻰→龙、⺠→民）
        统一成标准汉字，避免"水⻰头"这类残留影响题库匹配。康熙部首表作为补充。
        """
        if not text:
            return text
        out = []
        for ch in text:
            name = f"uni{ord(ch):X}"
            glyph_hash = self.glyph_map.get(name)
            original = self.hash2char.get(glyph_hash) if glyph_hash else None
            char = _name_to_char(original) if original else None
            out.append(char if char else ch)
        # 只用显式部首表还原（不用 NFKC：实测它对这类部首无效，还会把全角标点转成半角）
        return "".join(out).translate(KX_RADICALS_TAB).translate(CJK_RADICAL_TAB)
