"""教学周次：把课次日期换算成「第几周」。

程序不知道学校校历，因此统一按「第 1 周从周一开始」推导，起始日按优先级取：

1. 用户在界面上填写的第 1 周起始日；
2. 课程接口返回的开课日期（``SessionInfo.meta["term_start"]``）；
3. 已列出的最早课次所在周 —— 此时最早一节就是第 1 周，属相对周次。

解析出的起始日一律对齐到周一，避免同一周内的课次被拆到两个周次。
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable

_DATE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})")
_EPOCH = re.compile(r"^\d{10}(?:\d{3})?$")

TERM_START_SOURCES = {
    "configured": "界面设置",
    "api": "课程接口",
    "inferred": "最早课次",
    "": "未知",
}


def parse_date(value: object) -> dt.date | None:
    """接受日期对象、``2026-11-09`` 这类文本，或 10/13 位时间戳。"""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _EPOCH.match(text):
        seconds = int(text) / (1000 if len(text) == 13 else 1)
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    found = _DATE.search(text)
    if not found:
        return None
    try:
        return dt.date(int(found[1]), int(found[2]), int(found[3]))
    except ValueError:
        return None


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def week_of(day: dt.date, term_start: dt.date) -> int:
    """第几周从 1 开始；早于起始日的课次也按第 1 周显示。"""
    return max(1, (monday_of(day) - monday_of(term_start)).days // 7 + 1)


def resolve_term_start(
    configured: object = None, hint: object = None, dates: Iterable[object] = ()
) -> tuple[dt.date | None, str]:
    """返回 ``(第 1 周周一, 依据)``；没有可用日期时返回 ``(None, "")``。"""
    for value, source in ((configured, "configured"), (hint, "api")):
        parsed = parse_date(value)
        if parsed:
            return monday_of(parsed), source
    known = sorted(day for day in (parse_date(item) for item in dates) if day)
    if known:
        return monday_of(known[0]), "inferred"
    return None, ""
