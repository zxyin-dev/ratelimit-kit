# -*- coding: utf-8 -*-
"""测试用的小工具。"""

from __future__ import annotations

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TMP_ROOT = PROJECT_ROOT / ".tmp"


def fresh_dir(name: str) -> Path:
    """项目内的一次性目录。

    刻意不用 ``tempfile``：这套测试要在受限沙箱里跑，
    而系统临时目录可能不可写。放在项目内还顺带保证 ``.gitignore`` 能盖住。
    """
    target = TMP_ROOT / name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    return target


class SleepRecorder:
    """替换真实 sleep：测试不用等，而且能**断言等待时长**。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)

    @property
    def total(self) -> float:
        return sum(self.calls)
