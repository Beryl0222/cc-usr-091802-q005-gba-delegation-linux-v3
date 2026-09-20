"""ISO-8601 时间工具。系统内部统一使用带时区的 ``datetime``。"""

from datetime import datetime, timedelta, timezone

DEFAULT_TZ = timezone(timedelta(hours=8), name="UTC+8")


def parse_ts(value):
    """把 ISO 字符串解析为带时区时间；裸时间按东八区处理。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=DEFAULT_TZ)
    return dt


def iso(dt):
    """序列化回 ISO-8601 字符串。"""
    if dt is None:
        return None
    return dt.astimezone(DEFAULT_TZ).isoformat(timespec="minutes")


def overlaps(start_a, end_a, start_b, end_b):
    """半开区间是否相交（端点相接不算冲突）。"""
    return start_a < end_b and start_b < end_a


def overlap_any(start, end, windows):
    """是否与任一窗口相交。"""
    return any(overlaps(start, end, s, e) for s, e in windows)


def fits_in(start, end, windows):
    """[start, end) 是否完整落在某一窗口内。"""
    return any(s <= start and end <= e for s, e in windows)
