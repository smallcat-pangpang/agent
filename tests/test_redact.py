"""Trace 脱敏测试：PII 必须被替换成稳定假名，且不得残留。

评估系统本身不能成为隐私风险源（规范 8.2）。
"""

from __future__ import annotations

import hashlib
import os
import re
import unittest
from unittest.mock import patch

from atk.config import PrivacyConfig
from atk.trace.redact import PSEUDONYM_HEX_LEN, Redactor, audit


class TestRedactor(unittest.TestCase):
    def setUp(self):
        self.redactor = Redactor(PrivacyConfig(salt="test-salt", enabled=True))

    def test_mobile_email_id_masked(self):
        text = "手机 13800138000，邮箱 user@example.com，身份证 110101199001011234"
        out = self.redactor.redact_text(text)
        self.assertNotIn("13800138000", out)
        self.assertNotIn("user@example.com", out)
        self.assertNotIn("110101199001011234", out)
        self.assertIn("«cn_mobile:", out)
        self.assertIn("«email:", out)

    def test_stable_pseudonym(self):
        first = self.redactor.redact_text("13800138000")
        second = self.redactor.redact_text("13800138000")
        self.assertEqual(first, second)

    def test_different_values_different_hash(self):
        a = self.redactor.redact_text("13800138000")
        b = self.redactor.redact_text("13912345678")
        self.assertNotEqual(a, b)

    def test_sensitive_key_replaced_whole(self):
        out = self.redactor.redact_obj({"phone": "anything", "case_id": "AGT-L1-0001"})
        self.assertNotEqual(out["phone"], "anything")
        self.assertIn("«phone:", out["phone"])
        self.assertEqual(out["case_id"], "AGT-L1-0001")

    def test_keep_keys(self):
        redactor = Redactor(
            PrivacyConfig(salt="s", enabled=True, keep_keys={"case_id"})
        )
        out = redactor.redact_obj({"case_id": "13800138000"})
        self.assertEqual(out["case_id"], "13800138000")

    def test_nested_structures(self):
        data = {"a": [{"email": "x@y.com"}], "b": {"token": "sk-abcdefghijklmnopqrst"}}
        out = self.redactor.redact_obj(data)
        self.assertNotIn("x@y.com", str(out))
        self.assertNotIn("sk-abcdefghijklmnopqrst", str(out))

    def test_disabled(self):
        redactor = Redactor(PrivacyConfig(enabled=False))
        text = "13800138000"
        self.assertEqual(redactor.redact_text(text), text)

    def test_audit_finds_pii(self):
        found = audit("联系 13800138000 或 a@b.com", PrivacyConfig(salt="s"))
        self.assertGreaterEqual(found.get("cn_mobile", 0), 1)
        self.assertGreaterEqual(found.get("email", 0), 1)

    def test_clean_text_has_no_findings(self):
        self.assertEqual(audit("已导出 12 条订单", PrivacyConfig(salt="s")), {})

    def test_pseudonym_uses_hmac_and_64bit(self):
        """P1-8：8 位裸 sha256 在已知盐下可被暴力反查，必须是 HMAC + 16 位。"""
        out = self.redactor.redact_text("13800138000")
        match = re.search(r"«cn_mobile:([0-9a-f]+)»", out)
        self.assertIsNotNone(match)
        digest = match.group(1)
        self.assertEqual(len(digest), PSEUDONYM_HEX_LEN)
        plain = hashlib.sha256(b"test-salt13800138000").hexdigest()[:PSEUDONYM_HEX_LEN]
        self.assertNotEqual(digest, plain, "不能退化成 sha256(salt+value)")

    def test_insecure_default_salt_is_detected(self):
        self.assertTrue(Redactor(PrivacyConfig()).has_insecure_salt)
        with patch.dict(os.environ, {"ATK_PRIVACY_SALT": "r" * 32}):
            self.assertFalse(Redactor(PrivacyConfig()).has_insecure_salt)

    def test_custom_pattern(self):
        redactor = Redactor(
            PrivacyConfig(
                salt="s", enabled=True, extra_patterns={"employee_id": r"EMP\d{6}"}
            )
        )
        out = redactor.redact_text("工号 EMP123456")
        self.assertNotIn("EMP123456", out)
        self.assertIn("«employee_id:", out)


if __name__ == "__main__":
    unittest.main()
