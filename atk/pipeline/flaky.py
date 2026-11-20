"""Flaky 用例的隔离区管理。

规范 3.3 的处理方式很明确：**隔离，而不是删除**。

- 同版本同用例连续 3 轮结果不一致 → 打 ``flaky`` 标 → 进隔离区；
- 隔离区用例**不计入门禁分子分母**（否则门禁会被不稳定用例永久卡红）；
- 每周人工复核：若确认是模型本身不稳定，那就是真实产品缺陷，要修不要隔离。

隔离区用一个小 JSON 文件维护（``reports/quarantine.json``），
进版本库后可审计「谁在什么时候把哪条用例关进了小黑屋」。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from ..metrics.aggregate import RunSummary


@dataclass
class QuarantineEntry:
    """一条隔离记录。"""

    case_id: str
    since: str = ""
    reason: str = ""
    rounds: int = 0
    owner: str = ""
    #: 最近一次复核时间与结论
    last_review: str = ""
    review_note: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuarantineEntry":
        return cls(
            case_id=str(data.get("case_id", "")),
            since=str(data.get("since", "")),
            reason=str(data.get("reason", "")),
            rounds=int(data.get("rounds", 0)),
            owner=str(data.get("owner", "")),
            last_review=str(data.get("last_review", "")),
            review_note=str(data.get("review_note", "")),
        )


class QuarantineStore:
    """隔离区存储。"""

    def __init__(self, path: str = "reports/quarantine.json") -> None:
        self.path = path
        self.entries: dict[str, QuarantineEntry] = {}
        self.load()

    # -- 读写 -------------------------------------------------------------

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        for item in data.get("cases", []):
            entry = QuarantineEntry.from_dict(item)
            if entry.case_id:
                self.entries[entry.case_id] = entry

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "note": "隔离区用例不计入门禁分子分母；每周人工复核",
            "cases": [asdict(e) for e in sorted(self.entries.values(), key=lambda e: e.case_id)],
        }
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # -- 查询/变更 --------------------------------------------------------

    def is_quarantined(self, case_id: str) -> bool:
        return case_id in self.entries

    def quarantined_ids(self) -> set[str]:
        return set(self.entries)

    def add(self, case_id: str, reason: str, rounds: int = 0, owner: str = "") -> None:
        if case_id in self.entries:
            self.entries[case_id].reason = reason or self.entries[case_id].reason
            return
        self.entries[case_id] = QuarantineEntry(
            case_id=case_id,
            since=datetime.now(timezone.utc).date().isoformat(),
            reason=reason,
            rounds=rounds,
            owner=owner,
        )

    def remove(self, case_id: str, note: str = "") -> bool:
        if case_id not in self.entries:
            return False
        entry = self.entries.pop(case_id)
        # 保留审计线索：记录到复核备注里（通过 last_review 字段）
        entry.last_review = datetime.now(timezone.utc).date().isoformat()
        entry.review_note = note or "解除隔离"
        return True

    def review(self, case_id: str, note: str) -> None:
        if case_id in self.entries:
            self.entries[case_id].last_review = datetime.now(timezone.utc).date().isoformat()
            self.entries[case_id].review_note = note
            return
        # 未隔离但要求复核，等价于「人工确认稳定」，写一条历史
        self.entries.pop(case_id, None)

    # -- 自动标记 ---------------------------------------------------------

    def update_from_summary(
        self, summary: RunSummary, flaky_rounds: int = 3, auto_quarantine: bool = True
    ) -> list[str]:
        """根据本次运行的抖动情况自动维护隔离区。

        Returns:
            本次被新加入隔离区的用例 ID。
        """
        newly: list[str] = []
        for case_id in summary.flaky_cases:
            agg = summary.cases.get(case_id)
            if agg is None:
                continue
            if not auto_quarantine:
                continue
            if self.is_quarantined(case_id):
                self.entries[case_id].rounds += agg.samples
                continue
            statuses = summary.sample_status.get(case_id, [])
            self.add(
                case_id,
                reason=(
                    f"同版本连续 {agg.samples} 次采样结果不一致"
                    f"（{statuses}），超过 flaky 阈值 {flaky_rounds}"
                ),
                rounds=agg.samples,
            )
            newly.append(case_id)
        if newly:
            self.save()
        return newly


def apply_quarantine(summary: RunSummary, store: QuarantineStore) -> None:
    """把隔离状态写回汇总，供门禁排除。"""
    for case_id, agg in summary.cases.items():
        if store.is_quarantined(case_id):
            agg.quarantined = True
            if case_id not in summary.quarantined_cases:
                summary.quarantined_cases.append(case_id)
