from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


POLICY_PATH = Path(__file__).with_name("policy.zh-CN.txt")


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    version: str
    text: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("隐私政策版本不能为空")
        if not self.text.strip():
            raise ValueError("隐私政策正文不能为空")


def load_privacy_policy(*, version: str) -> PrivacyPolicy:
    return PrivacyPolicy(
        version=version.strip(),
        text=POLICY_PATH.read_text(encoding="utf-8").strip(),
    )

