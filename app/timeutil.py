"""ISO8601 时间窗工具：全部时间使用带时区偏移的本地时间字符串。

时间格式::

    2026-09-12T09:00:00+08:00

为便于测试，所有“当前时间”由可注入的时钟决定，绝不在领域逻辑里直接
``datetime.now()``。
"""

from datetime import datetime, timedelta, timezone

DEFAULT_TZ = timezone(timedelta(hours=8))  # 粤港澳统一使用 UTC+8


def parse_ts(value):
    """解析时间字符串；naive 时间按 UTC+8 处理。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=DEFAULT_TZ)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt


def iso(dt):
    return dt.astimezone(DEFAULT_TZ).isoformat(timespec="minutes")


def now_iso(clock):
    """:class:`datetime` 或返回时间字符串/时间对象的零参时钟。"""
    value = clock() if callable(clock) else clock
    if isinstance(value, str):
        value = parse_ts(value)
    return iso(value)


def parse_window(spec):
    """把 ``{"start", "end"}`` 解析为时间元组。"""
    return parse_ts(spec["start"]), parse_ts(spec["end"])


def overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def shift(window, minutes):
    """整体平移一个时间窗。"""
    return window[0] + timedelta(minutes=minutes), window[1] + timedelta(minutes=minutes)


def mins(minutes):
    return timedelta(minutes=minutes)
