"""Trace 落盘前的 PII 脱敏。

规范 8.2：评估系统本身不能成为隐私风险源。Trace 里必然出现用户原话、
订单号、手机号、邮箱，直接落盘就是把测试环境的隐私复制了一份。

脱敏策略：
1. **可逆性不需要**：评估只需要「同一实体的多次出现能对上」，所以用
   ``HMAC-SHA256(key, 原文)[:16]`` 生成稳定假名，同一个手机号永远得到同一个假名。
   注意：摘要截断到 8 位（32 bit）时，手机号空间仅约 1.4×10⁹，在**盐值已知**
   的情况下可以分钟级全量建表反查 —— 所以这里改用 HMAC + 16 位（64 bit），
   并且盐值必须保密、不能用仓库里的默认值；生产环境通过 ``ATK_PRIVACY_SALT``
   环境变量注入，:attr:`Redactor.has_insecure_salt` 会在落盘前告警；
2. **只脱敏不删除**：保留占位符，便于统计「一条 trace 里出现了几处手机号」；
3. **落盘前统一走一遍**，并提供 :func:`audit` 在 CI 里做泄漏巡检。

支持：邮箱、中国大陆手机号、身份证、银行卡、IPv4、API Key、Bearer Token、
以及配置里的自定义正则。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from ..config import PrivacyConfig

#: 内置 PII 规则（name -> 正则）。顺序有意义：先匹配更具体的模式。
#: 摘要长度（hex 字符）。8 位=32bit 对手机号空间太小，容易被暴力建表反查。
PSEUDONYM_HEX_LEN = 16

#: 仓库里出现过的示例/默认盐值，命中即视为「未配置」，必须告警
INSECURE_SALTS: frozenset[str] = frozenset(
    {"", "atk-default-salt-change-me", "atk-demo-salt-please-change"}
)

#: 生产环境通过该环境变量注入盐值，避免把密钥写进仓库
SALT_ENV_VAR = "ATK_PRIVACY_SALT"

BUILTIN_PATTERNS: dict[str, str] = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "cn_id": r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b",
    "cn_mobile": r"(?<!\d)1[3-9]\d{9}(?!\d)",
    "bank_card": r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)",
    "ipv4": r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)",
    "api_key": r"(?:sk|ak|pk)-[A-Za-z0-9]{16,}",
    "bearer": r"(?i)bearer\s+[A-Za-z0-9._\-]{10,}",
    "url_token": r"(?i)(?:token|access_token|apikey|api_key)=([A-Za-z0-9._\-]{8,})",
}

#: 邮箱/手机号等常见字段名，值需要整体替换（即使值本身不匹配正则）
SENSITIVE_KEYS = {
    "phone",
    "mobile",
    "email",
    "id_card",
    "idcard",
    "password",
    "token",
    "api_key",
    "apikey",
    "secret",
    "authorization",
}


@dataclass
class RedactionRecord:
    """一条脱敏记录（用于审计：脱敏了什么、多少处）。"""

    pii_type: str
    count: int
    placeholder_sample: str = ""


class Redactor:
    """把对象里的 PII 替换成稳定假名。"""

    def __init__(self, config: PrivacyConfig | None = None) -> None:
        self.config = config or PrivacyConfig()
        self.patterns: dict[str, re.Pattern[str]] = {
            name: re.compile(pattern)
            for name, pattern in {**BUILTIN_PATTERNS, **self.config.extra_patterns}.items()
        }
        self.counts: dict[str, int] = {}
        # Runner 用 ThreadPoolExecutor 并发采样，counts 的 += 是读改写，必须加锁
        self._lock = threading.Lock()
        self._key = self._resolve_key()

    def _resolve_key(self) -> bytes:
        env_salt = os.environ.get(SALT_ENV_VAR, "")
        salt = env_salt or self.config.salt or ""
        return salt.encode("utf-8")

    @property
    def has_insecure_salt(self) -> bool:
        """是否仍在使用仓库里的示例盐值（生产落盘 trace 前必须消除）。"""
        env_salt = os.environ.get(SALT_ENV_VAR, "")
        configured = env_salt or self.config.salt or ""
        return configured.strip() in INSECURE_SALTS or len(configured) < 16

    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counts[name] = self.counts.get(name, 0) + amount

    # -- 文本级 -----------------------------------------------------------

    def _fake(self, value: str) -> str:
        # HMAC 而非裸 sha256：即使攻击者拿到假名，也无法用「已知盐 + 字典」反查
        return hmac.new(self._key, value.encode("utf-8"), hashlib.sha256).hexdigest()[
            :PSEUDONYM_HEX_LEN
        ]

    def redact_text(self, text: str) -> str:
        """替换文本中的 PII，返回脱敏后的文本。"""
        if not self.config.enabled or not text:
            return text

        result = text
        for name, pattern in self.patterns.items():
            def _replace(match: re.Match[str], _name: str = name) -> str:
                raw = match.group(0)
                # url_token 只替换捕获组，保留参数名，便于看懂上下文
                if _name == "url_token" and match.lastindex:
                    raw = match.group(1)
                    self._bump(_name)
                    return match.group(0).replace(raw, f"«{_name}:{self._fake(raw)}»")
                self._bump(_name)
                return f"«{_name}:{self._fake(raw)}»"

            result = pattern.sub(_replace, result)
        return result

    # -- 对象级 -----------------------------------------------------------

    def redact_obj(self, value: Any, key: str = "") -> Any:
        """递归脱敏 dict/list/str。

        - 命中敏感字段名（如 ``phone``）：整值替换；
        - 字符串：走文本正则；
        - ``keep_keys`` 中的字段跳过（例如 case_id、tool）。
        """
        if not self.config.enabled:
            return value
        if key and key.lower() in self.config.keep_keys:
            return value
        if isinstance(value, dict):
            return {k: self.redact_obj(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact_obj(v, key) for v in value]
        if isinstance(value, str):
            if key.lower() in SENSITIVE_KEYS:
                self._bump(key.lower())
                return f"«{key.lower()}:{self._fake(value)}»"
            return self.redact_text(value)
        return value

    def stats(self) -> dict[str, int]:
        return dict(self.counts)


def audit(text: str, config: PrivacyConfig | None = None) -> dict[str, int]:
    """巡检：返回文本中各类 PII 的出现次数（CI 里对落盘文件跑一遍）。

    与 :class:`Redactor` 共用规则，避免「脱敏规则」和「检测规则」两套标准。
    """
    redactor = Redactor(config)
    redactor.redact_text(text)
    return redactor.stats()


def assert_clean(text: str, config: PrivacyConfig | None = None) -> None:
    """确认文本里没有残留 PII（脱敏后自检）。"""
    found = audit(text, config)
    if found:
        raise ValueError(f"落盘内容仍含 PII: {found}")
