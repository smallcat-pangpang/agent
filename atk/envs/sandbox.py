"""每次采样一个干净沙箱：隔离文件系统、隔离产物、可溯源。

规范 6.1 要求「沙箱隔离：每次运行用干净环境，避免状态污染」。
本模块把一次采样需要的一切文件都收进一个目录：

.. code-block:: text

    <run_dir>/sandboxes/AGT-ORD-0031/s1/
      ├─ work/        # Agent 的可写工作目录，交付物落在这里
      ├─ inputs/      # 用例附件副本（只读语义，框架不强制）
      └─ meta.json    # 沙箱指纹：case_id / sample / 冻结时间 / 种子

产物断言（file_exists / state_change）只在 ``work/`` 内查找，
防止误把框架自己的文件当成 Agent 的交付物。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Sandbox:
    """单次采样的文件系统沙箱。"""

    root: str
    case_id: str
    sample_idx: int
    frozen_time: str = ""
    seed: int = 0
    #: 运行期间对 work/ 内文件的校验和快照（用于 state_change 的 modified 判定）
    initial_hashes: dict[str, str] = field(default_factory=dict)

    # -- 生命周期 ---------------------------------------------------------

    @classmethod
    def create(
        cls,
        base_dir: str,
        case_id: str,
        sample_idx: int,
        frozen_time: str = "",
        seed: int = 0,
        clean: bool = True,
    ) -> "Sandbox":
        root = os.path.join(base_dir, case_id, f"s{sample_idx}")
        if clean and os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(os.path.join(root, "work"), exist_ok=True)
        os.makedirs(os.path.join(root, "inputs"), exist_ok=True)
        box = cls(
            root=root,
            case_id=case_id,
            sample_idx=sample_idx,
            frozen_time=frozen_time,
            seed=seed,
        )
        box.write_meta()
        return box

    def write_meta(self) -> None:
        meta = {
            "case_id": self.case_id,
            "sample_idx": self.sample_idx,
            "frozen_time": self.frozen_time,
            "seed": self.seed,
        }
        with open(os.path.join(self.root, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

    def cleanup(self) -> None:
        """删除沙箱（默认保留，便于人肉复核失败现场；由 CLI 的 --keep 控制）。"""
        shutil.rmtree(self.root, ignore_errors=True)

    # -- 路径 -------------------------------------------------------------

    @property
    def work_dir(self) -> str:
        return os.path.join(self.root, "work")

    @property
    def input_dir(self) -> str:
        return os.path.join(self.root, "inputs")

    def work_path(self, relative: str) -> str:
        """把相对路径解析到 work/ 内。

        做了「路径逃逸」防护：``../../etc/passwd`` 会抛错，
        避免被测 Agent 通过构造路径写到沙箱外。
        """
        target = os.path.abspath(os.path.join(self.work_dir, relative))
        if os.path.commonpath([target, os.path.abspath(self.work_dir)]) != os.path.abspath(
            self.work_dir
        ):
            raise ValueError(f"路径越出沙箱: {relative}")
        return target

    # -- 文件操作 ---------------------------------------------------------

    def stage_attachment(self, source: str, relative: str | None = None) -> str:
        """把用例附件复制进 inputs/（保留相对路径），返回沙箱内路径。"""
        rel = relative or os.path.basename(source)
        target = os.path.join(self.input_dir, rel)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        shutil.copyfile(source, target)
        return target

    def write_text(self, relative: str, content: str) -> str:
        target = self.work_path(relative)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        return target

    def write_bytes(self, relative: str, content: bytes) -> str:
        target = self.work_path(relative)
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(content)
        return target

    def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    # -- 快照 -------------------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        """对 work/ 下所有文件做哈希快照（相对路径 -> sha256）。"""
        hashes: dict[str, str] = {}
        for rel in self.list_files():
            full = os.path.join(self.work_dir, rel)
            try:
                with open(full, "rb") as fh:
                    hashes[rel] = hashlib.sha256(fh.read()).hexdigest()
            except OSError:
                continue
        return hashes

    def mark_initial(self) -> None:
        self.initial_hashes = self.snapshot()

    def list_files(self, pattern: str = "*") -> list[str]:
        """列出 work/ 内的文件（相对路径，正斜杠），支持 ``**`` 递归与 glob。"""
        import fnmatch

        results: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self.work_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self.work_dir).replace(os.sep, "/")
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    results.append(rel)
        return sorted(results)

    def artifacts(self) -> list[str]:
        """交付物清单（work/ 内所有非空文件）。"""
        out: list[str] = []
        for rel in self.list_files():
            full = os.path.join(self.work_dir, rel)
            try:
                if os.path.getsize(full) > 0:
                    out.append(rel)
            except OSError:
                continue
        return out

    def fingerprint(self) -> dict[str, Any]:
        """环境指纹：进运行记录，用来复现「到底是环境变了还是模型变了」。"""
        return {
            "root": self.root,
            "frozen_time": self.frozen_time,
            "seed": self.seed,
            "files": len(self.list_files()),
        }
