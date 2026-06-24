"""字符串替换的多级容错匹配模块。

为 `api/file/replace` 提供严格度递减的多策略回退链，命中即止：

1. exact              —— 精确逐字符相等
2. rstrip_line        —— 逐行右侧 trim（容忍行尾空白）
3. strip_line         —— 逐行两侧 trim（容忍缩进差异）
4. unicode_normalized —— Unicode 归一化（花引号/破折号/不间断空格 → ASCII）后精确
5. block_anchor       —— ≥3 行块锚定首尾行 + 中间行相似度（容忍块内细微差异）

两道防止「容错过度」的保险：
- 唯一性强制：命中多处且非 replace_all 时拒绝（not_unique）。
- 跨度保险：模糊匹配跨度远大于 old_str 时拒绝（disproportionate）。

匹配函数返回的是在「原始 content 上的字符区间」，调用方按区间做替换，
因此即便比较时做了 trim/归一化，写回的也是原文实际内容之外的部分保持不变。
"""

from difflib import SequenceMatcher

# 各策略的人类可读名称（用于 not_found 时回传「已尝试的容错级别」）
STRATEGY_LABELS: dict[str, str] = {
    "exact": "精确匹配",
    "rstrip_line": "行尾空白容错",
    "strip_line": "缩进容错",
    "unicode_normalized": "Unicode 归一化",
    "block_anchor": "块锚定相似匹配",
}

# block_anchor 中间行的相似度阈值
_BLOCK_SIMILARITY_THRESHOLD = 0.65
# block_anchor 允许的块大小相对偏差（±25%）
_BLOCK_SIZE_TOLERANCE = 0.25

# 长度保持的 Unicode → ASCII 归一化映射（每个字符映射为恰好 1 个 ASCII 字符，
# 因此在归一化后的文本中找到的下标可直接用于原始文本）。
_UNICODE_MAP: dict[int, str] = {
    # 单引号类
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'",
    # 双引号类
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"', 0x2033: '"',
    # 破折号/连字符类
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-",
    0x2015: "-", 0x2212: "-",
    # 各类不间断/特殊空格
    0x00A0: " ", 0x1680: " ", 0x2000: " ", 0x2001: " ", 0x2002: " ",
    0x2003: " ", 0x2004: " ", 0x2005: " ", 0x2006: " ", 0x2007: " ",
    0x2008: " ", 0x2009: " ", 0x200A: " ", 0x202F: " ", 0x205F: " ",
    0x3000: " ",
}


class MatchError(Exception):
    """容错匹配失败异常，携带可结构化透传的原因。

    属性:
        reason: 机器可读原因码（not_found / not_unique / disproportionate）
        detail: 人类可读说明（直接可被模型与审计消费）
        info: 附加结构化字段（如命中处数、行号、已尝试策略等）
    """

    def __init__(self, reason: str, detail: str, **info):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.info = info


def _line_offsets(content: str) -> tuple[list[str], list[int]]:
    """返回 (按 \n 切分的行列表, 每行起始字符下标列表)。"""
    lines = content.split("\n")
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1  # +1 为换行符
    return lines, offsets


def _exact_spans(content: str, old_str: str) -> list[tuple[int, int]]:
    """精确（逐字符）匹配，返回非重叠区间列表。"""
    if not old_str:
        return []
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        i = content.find(old_str, start)
        if i == -1:
            break
        spans.append((i, i + len(old_str)))
        start = i + len(old_str)
    return spans


def _split_old_lines(old_str: str) -> tuple[list[str], bool]:
    """切分 old_str 为行，并返回是否以换行结尾。"""
    lines = old_str.split("\n")
    trailing_newline = False
    if len(lines) > 1 and lines[-1] == "":
        lines = lines[:-1]
        trailing_newline = True
    return lines, trailing_newline


def _line_window_spans(content: str, old_str: str, transform) -> list[tuple[int, int]]:
    """逐行窗口匹配：对每行施加 transform 后比较，返回原文字符区间。

    用于行尾 trim / 两侧 trim 等「整行块」级别的容错；不处理行内子串。
    """
    old_lines, trailing_newline = _split_old_lines(old_str)
    if not old_lines:
        return []

    content_lines, offsets = _line_offsets(content)
    n, m = len(content_lines), len(old_lines)
    if m > n:
        return []

    old_t = [transform(l) for l in old_lines]
    spans: list[tuple[int, int]] = []
    i = 0
    while i <= n - m:
        if all(transform(content_lines[i + k]) == old_t[k] for k in range(m)):
            last = i + m - 1
            start = offsets[i]
            end = offsets[last] + len(content_lines[last])
            # old_str 以换行结尾时，若后面还有行，则把该换行也纳入区间
            if trailing_newline and last < n - 1:
                end += 1
            spans.append((start, end))
            i += m  # 非重叠推进
        else:
            i += 1
    return spans


def _normalize_unicode(text: str) -> str:
    """长度保持的 Unicode → ASCII 归一化。"""
    return text.translate(_UNICODE_MAP)


def _unicode_spans(content: str, old_str: str) -> list[tuple[int, int]]:
    """先做长度保持的 Unicode 归一化，再精确匹配。

    由于归一化逐字符 1:1，归一化文本中的下标可直接用于原始 content。
    """
    return _exact_spans(_normalize_unicode(content), _normalize_unicode(old_str))


def _block_anchor_spans(content: str, old_str: str) -> list[tuple[int, int]]:
    """块锚定匹配：对 ≥3 行的块，锚定首尾行（trim 后相等），
    中间行整体相似度达阈值，且块大小在 ±25% 容差内。
    """
    old_lines, trailing_newline = _split_old_lines(old_str)
    m = len(old_lines)
    if m < 3:
        return []

    first = old_lines[0].strip()
    last = old_lines[-1].strip()
    if not first or not last:
        return []  # 锚定行必须是非空内容行

    content_lines, offsets = _line_offsets(content)
    n = len(content_lines)
    tolerance = max(1, round(m * _BLOCK_SIZE_TOLERANCE))
    old_middle = "\n".join(l.strip() for l in old_lines[1:-1])

    spans: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if content_lines[i].strip() != first:
            i += 1
            continue
        # 在大小容差窗口内寻找匹配尾锚的行
        matched_j = -1
        for j in range(i + 2, min(n, i + m + tolerance)):
            block_size = j - i + 1
            if abs(block_size - m) > tolerance:
                continue
            if content_lines[j].strip() != last:
                continue
            cand_middle = "\n".join(l.strip() for l in content_lines[i + 1:j])
            ratio = SequenceMatcher(None, old_middle, cand_middle).ratio()
            if ratio >= _BLOCK_SIMILARITY_THRESHOLD:
                matched_j = j
                break
        if matched_j == -1:
            i += 1
            continue
        start = offsets[i]
        end = offsets[matched_j] + len(content_lines[matched_j])
        if trailing_newline and matched_j < n - 1:
            end += 1
        spans.append((start, end))
        i = matched_j + 1  # 非重叠推进

    return spans


# 严格度递减的策略链
_STRATEGIES = [
    ("exact", _exact_spans),
    ("rstrip_line", lambda c, o: _line_window_spans(c, o, str.rstrip)),
    ("strip_line", lambda c, o: _line_window_spans(c, o, str.strip)),
    ("unicode_normalized", _unicode_spans),
    ("block_anchor", _block_anchor_spans),
]

# 仅对这些「模糊」策略施加跨度保险
_FUZZY_STRATEGIES = {"block_anchor"}


def _is_disproportionate(span_len: int, old_len: int) -> bool:
    """匹配跨度相对 old_str 是否过大（防止模糊匹配吞掉无关内容）。"""
    return span_len > max(old_len * 3, old_len + 200)


def _occurrence_lines(content: str, spans: list[tuple[int, int]]) -> list[int]:
    """计算每个匹配区间起始位置所在行号（1-based）。"""
    return [content[: s].count("\n") + 1 for s, _ in spans]


def find_replacement_spans(
    content: str, old_str: str, replace_all: bool = False
) -> tuple[list[tuple[int, int]], str]:
    """在 content 中定位 old_str 的替换区间。

    按策略链严格度递减依次尝试，命中即止。

    返回:
        (spans, strategy): spans 为原文中需替换的非重叠字符区间（已排序），
        strategy 为命中的策略名。

    抛出:
        MatchError: 未找到 / 不唯一 / 跨度异常。
    """
    if old_str == "":
        raise MatchError("not_found", "old_str 为空，无法定位替换位置。")

    for name, fn in _STRATEGIES:
        spans = fn(content, old_str)
        if not spans:
            continue

        # 保险一：唯一性强制
        if len(spans) > 1 and not replace_all:
            lines = _occurrence_lines(content, spans)
            raise MatchError(
                "not_unique",
                f"old_str 不唯一：经「{STRATEGY_LABELS[name]}」在第 {lines} 行命中 "
                f"{len(spans)} 处。请扩大上下文使其唯一，或设置 replace_all=true 全部替换。",
                occurrences=len(spans),
                lines=lines,
                strategy=name,
            )

        # 保险二：跨度保险（仅模糊策略）
        if name in _FUZZY_STRATEGIES:
            for s, e in spans:
                if _is_disproportionate(e - s, len(old_str)):
                    raise MatchError(
                        "disproportionate",
                        f"匹配被拒：经「{STRATEGY_LABELS[name]}」命中区间跨度 "
                        f"({e - s} 字符) 远大于 old_str ({len(old_str)} 字符)，"
                        f"疑似模糊匹配吞入无关内容。请提供更精确的 old_str。",
                        span=e - s,
                        old_len=len(old_str),
                        strategy=name,
                    )

        return spans, name

    tried = "、".join(STRATEGY_LABELS[name] for name, _ in _STRATEGIES)
    raise MatchError(
        "not_found",
        f"old_str 在文件中未找到（已依次尝试：{tried}）。"
        f"建议先用 file_read 查看文件最新内容后再重试。",
        tried=[name for name, _ in _STRATEGIES],
    )


def apply_spans(content: str, spans: list[tuple[int, int]], new_str: str) -> str:
    """按给定区间将 content 中对应内容替换为 new_str。区间需非重叠且已排序。"""
    parts: list[str] = []
    prev = 0
    for s, e in spans:
        parts.append(content[prev:s])
        parts.append(new_str)
        prev = e
    parts.append(content[prev:])
    return "".join(parts)
