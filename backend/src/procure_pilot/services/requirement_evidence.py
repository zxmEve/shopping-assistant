"""Small deterministic proofs of current intent and numeric unit conversions.

All returned offsets refer to the original text. Unknown/ambiguous statements are
left unresolved; these helpers do not infer a requirement from background facts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal


def clause_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = max((m.end() for m in re.finditer(r"[,，。;；\n]", text[:start])), default=0)
    right = re.search(r"[,，。;；\n]", text[end:])
    return left, end + right.start() if right else len(text)


def is_background(text: str, start: int, end: int) -> bool:
    left, right = clause_bounds(text, start, end)
    clause = text[left:right]
    prefix = text[left:start]
    if re.search(r"保留|沿用|维持|保持", clause) and not re.search(
        r"不(?:再)?(?:保留|沿用|维持|保持)", clause
    ):
        return False
    background = re.search(
        r"上次|去年|历史|之前|原来|原先|仓库|库存|(?:另|还)有|"
        r"已(?:经)?.*(?:买完|采购完)|(?:建议|推荐).*(?:买|采购)",
        clause,
    )
    if not background:
        return False
    current = list(re.finditer(r"本次|这次|现在|我确认|最终", prefix))
    return not current or current[-1].start() < background.start()


def current_matches(pattern: str, text: str) -> list[re.Match[str]]:
    return [m for m in re.finditer(pattern, text) if not is_background(text, *m.span())]


def quantity_candidates(text: str) -> list[re.Match[str]]:
    return current_matches(r"(?<![\d.])(\d+)\s*(?:台|件)", text)


def quantity_mention(text: str) -> re.Match[str] | None:
    matches = quantity_candidates(text)
    corrections = []
    for match in matches:
        left, _ = clause_bounds(text, *match.span())
        prefix = text[left : match.start()]
        if re.search(
            r"(?:改为|改成|调整为|调整到|(?:最终|本次|这次|现在|我确认)"
            r"(?:只|要|确认|采购|购买|买|需要|采购数量|数量|为|是|：|:|\s)*)\s*$",
            prefix,
        ) or (prefix.rstrip().endswith("以") and re.match(r"\s*为准", text[match.end() :])):
            corrections.append(match)
    if corrections:
        return corrections[-1]
    if len({int(m.group(1)) for m in matches}) == 1:
        return matches[0]
    return None


def chinese_number(text: str) -> Decimal:
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return Decimal(text)
    total = section = number = 0
    for char in text:
        if char in "零一二三四五六七八九两":
            number = {
                "零": 0,
                "一": 1,
                "二": 2,
                "三": 3,
                "四": 4,
                "五": 5,
                "六": 6,
                "七": 7,
                "八": 8,
                "九": 9,
                "两": 2,
            }[char]
        elif char == "万":
            total += (section + number or 1) * 10000
            section = number = 0
        else:
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
    return Decimal(total + section + number)


@dataclass(frozen=True)
class CapacityMention:
    start: int
    end: int
    field: str
    value: int
    operator: str
    soft: bool


def capacity_mentions(text: str, *, default_field: str | None = None) -> list[CapacityMention]:
    lowered = text.lower()
    result = []
    previous_field = default_field
    previous_end = 0
    for match in re.finditer(
        r"(?<![\d.a-z])(\d+(?:\.\d+)?)\s*(tb|gb|t|g)(?![a-z])",
        lowered,
    ):
        if is_background(lowered, *match.span()):
            continue
        value = Decimal(match.group(1)) * (1024 if match.group(2).startswith("t") else 1)
        if value <= 0 or value != value.to_integral_value():
            continue
        left, right = clause_bounds(lowered, *match.span())
        prefix = lowered[max(left, previous_end) : match.start()]
        suffix = lowered[match.end() : right]
        # Adjacent suffix wins, then a preceding field marker in this clause.
        after = re.match(r"\s*(内存|ram|存储|硬盘|ssd)", suffix)
        before = list(re.finditer(r"内存|ram|存储|硬盘|ssd", prefix))
        marker = after.group(1) if after else before[-1].group(0) if before else None
        if marker:
            field = "minimum_ram_gb" if marker in {"内存", "ram"} else "minimum_storage_gb"
        elif match.group(2).startswith("t"):
            field = "minimum_storage_gb"
        elif previous_field and re.search(r"又|同时|最多|不超过|至少|最低", prefix):
            field = previous_field
        else:
            field = default_field or "minimum_ram_gb"
        field_prefix = prefix[before[-1].start() :] if before else prefix
        soft = bool(re.search(r"最好|优先|倾向|希望|更好", field_prefix + suffix[:2]))
        bounds = list(re.finditer(r"最多|不超过|至多|上限|至少|最低|下限", field_prefix))
        operator = (
            "LTE" if bounds and bounds[-1].group(0) in {"最多", "不超过", "至多", "上限"} else "GTE"
        )
        # Include only the adjacent qualifier/field and unit, not unrelated values.
        start = max(left, previous_end)
        end = match.end() + (after.end() if after else 0)
        result.append(CapacityMention(start, end, field, int(value), operator, soft))
        previous_field, previous_end = field, end
    return result


def warranty_mentions(text: str) -> list[tuple[re.Match[str], int]]:
    result = []
    pattern = (
        r"(?:保修|质保)\s*(?:至少|最低|不少于)?\s*"
        r"(?P<number>\d+(?:\.\d+)?|[零一二三四五六七八九十百两]+)\s*"
        r"(?P<unit>年|个月|月)(?P<half>半)?"
    )
    for match in current_matches(pattern, text):
        number = chinese_number(match.group("number"))
        if match.group("half"):
            number += Decimal(".5")
        months = number * (12 if match.group("unit") == "年" else 1)
        if months > 0 and months == months.to_integral_value():
            result.append((match, int(months)))
    return result


def budget_retractions(text: str) -> list[re.Match[str]]:
    result = []
    for match in re.finditer(r"[^,，。;；\n]+", text):
        clause = match.group(0)
        if is_background(text, *match.span()) and not re.search(
            r"^(?:取消|撤销)(?:原先|原来)|^(?:原先|原来).*(?:作废|取消|撤销)", clause
        ):
            continue
        if not re.search(r"预算|单价|单台.{0,8}上限|单件.{0,8}上限", clause):
            continue
        if re.search(r"建议|提议|考虑", clause) and not re.search(
            r"我确认|本次确认|现在确定", clause
        ):
            continue
        if re.search(r"如果|假如|若|(?:不|没)(?:要|能|会|需|有)?(?:撤销|取消|作废)", clause):
            continue
        if re.search(
            r"作废|撤销|取消|不再沿用|未批准|没批准|未确定|没确定|待定|待批准|待确认", clause
        ):
            result.append(match)
    return result
