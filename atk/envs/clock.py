"""冻结时钟：让「今天」「本周」这类相对时间在每次运行中完全一致。

规范 6.1 把「时间冻结」列为强制项：如果用例里出现「导出本周订单」，
而框架用系统当前时间，那么同一条用例在不同日期跑出的结果不同，
指标波动会被误判成模型退化。

实现上不做「拦截所有 datetime.now()」这种魔法（Python 里拦不全，容易给人虚假安全感），
而是：**把冻结时间显式注入给 Agent 适配器与工具 Mock**，并记录在运行指纹里。
真实 Agent 若绕过注入直接读系统时间，属于被测系统的问题，框架会在
``env_fingerprint`` 与报告里暴露这一点（便于排查「不可复现」投诉）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..model import parse_iso


class FrozenClock:
    """可推进的冻结时钟。

    - :meth:`now` 返回「冻结起点 + 已推进时长」；
    - :meth:`advance` 由工具 Mock 在模拟耗时后调用，让轨迹内的时间自洽；
    - 默认时区为 UTC，但保留原字符串的时区偏移（如 +08:00）。
    """

    def __init__(self, frozen_time: str) -> None:
        self._start = parse_iso(frozen_time)
        self._offset = timedelta(0)

    # -- 读取 -------------------------------------------------------------

    def now(self) -> datetime:
        return self._start + self._offset

    def iso(self) -> str:
        """ISO 8601 字符串（保留时区），写进运行记录。"""
        return self.now().isoformat()

    def date(self) -> str:
        return self.now().date().isoformat()

    def weekday(self) -> int:
        """ISO 星期：周一=1 ... 周日=7。"""
        return self.now().isoweekday()

    # -- 推进 -------------------------------------------------------------

    def advance(self, ms: int | float) -> None:
        """推进时钟（毫秒）。工具响应耗时应当推进，保证时间线自洽。"""
        if ms > 0:
            self._offset += timedelta(milliseconds=float(ms))

    def reset(self) -> None:
        self._offset = timedelta(0)

    @property
    def start_iso(self) -> str:
        return self._start.isoformat()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f"FrozenClock(start={self._start.isoformat()}, offset={self._offset})"


def utcnow_iso() -> str:
    """框架自身的时间戳（不是用例时间），用于 run_id / 报告生成时间。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
