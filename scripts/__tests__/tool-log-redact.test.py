#!/usr/bin/env python3
"""Regression coverage for TOOLLOGREDACT924 and TOOLLOGURLSCHEME924.

All fabricated secret bodies are assembled at run time so this source itself
does not become a false positive for a repository secret scanner.
"""
import importlib.util
import os
import unittest


HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(os.path.dirname(HERE), "hooks", "tool-log-capture.py")
spec = importlib.util.spec_from_file_location("tool_log_capture", HOOK)
hook = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(hook)

REDACTED = "[REDACTED]"
OPAQUE = "q9W8e7R6t5Y4u3I2" + "o1P0aSdFgH"
SBP = "sbp" + "_" + "a1b2c3d4e5" * 4
JWT = "ey" + "J" + "hbGciOiJIUzI1NiJ9" + ".ey" + "J" + "yb2xlIjoic2VydmljZSJ9" + "." + "c2lnbmF0dXJlZmFrZXh5"
BASIC = "ZmFrZXVzZXI6" + "ZmFrZXBhc3N3b3Jk"
STRIPE = "sk" + "_live_" + "51Hx" + "Ab9Cd8Ef7Gh6Ij5Kl4"
TELEGRAM = "7" + "123456789" + ":" + "AAF" + "k9Lm8Nb7Vc6Xz5Aq4Sw3De2Fr1Gt0Hy"
AWS_ID = "AK" + "IA" + "Q7W6E5R4T3Y2U1I0"


class ToolLogRedactionTests(unittest.TestCase):
    def assert_redacted(self, text, secret):
        output = hook._redact(text)
        self.assertNotIn(secret, output)
        self.assertIn(REDACTED, output)

    def test_quoted_named_and_spaced_values_are_redacted(self):
        self.assert_redacted('export SUPABASE_ACCESS_TOKEN="%s"' % SBP, SBP)
        self.assert_redacted("deploy --token %s" % OPAQUE, OPAQUE)
        self.assert_redacted("SERVICE_ROLE_KEY=%s node script.js" % JWT, JWT)
        self.assert_redacted('{"password": "%s"}' % OPAQUE, OPAQUE)

    def test_authorization_and_known_tokens_are_redacted(self):
        self.assert_redacted("Authorization: Basic %s" % BASIC, BASIC)
        self.assert_redacted("curl -u %s:" % STRIPE, STRIPE)
        self.assert_redacted("curl https://api.telegram.org/bot%s/getMe" % TELEGRAM, TELEGRAM.split(":", 1)[1])
        self.assert_redacted("aws configure set aws_access_key_id %s" % AWS_ID, AWS_ID)
        self.assert_redacted("echo %s" % JWT, JWT)

    def test_tool_specific_password_spellings_are_redacted(self):
        self.assert_redacted("sshpass -p %s ssh user@example.org" % OPAQUE, OPAQUE)
        self.assert_redacted("mysql -u root -p'%s' app" % OPAQUE, OPAQUE)
        self.assert_redacted('mysqldump -uroot -p"%s" app' % OPAQUE, OPAQUE)

    def test_url_credentials_are_redacted_for_http_and_non_http_schemes(self):
        for url in (
            "https://app:%s@example.org/repo" % OPAQUE,
            "postgresql://app:%s@db.example.org:5432/app" % OPAQUE,
            "redis://:%s@cache.example.org:6379" % OPAQUE,
            "amqp://guest:%s@mq.example.org:5672/" % OPAQUE,
            "mongodb+srv://user:%s@cluster.example.net/db" % OPAQUE,
            "rediss://%s@cache.example.org" % OPAQUE,
            "postgres://user:fake@%s@db.example.org/app" % OPAQUE,
        ):
            self.assert_redacted(url, OPAQUE)

    def test_safe_variable_references_and_non_secrets_are_preserved(self):
        keep = (
            'export TOKEN="$SUPABASE_TOKEN"',
            'deploy --token "$ACCESS_TOKEN"',
            "redis://:$REDIS_PASS@cache.example.org",
            "git clone ssh://git@github.com/org/repo.git",
            "mkdir -p /tmp/a-dir",
            "ssh -p 2222 example.org",
            "mysql -u root -p app",
        )
        for text in keep:
            self.assertEqual(hook._redact(text), text)

    def test_every_logged_summary_uses_the_same_redactor(self):
        command = "deploy --token %s" % OPAQUE
        summary = hook._input_summary({"command": command}, "Bash")
        self.assertNotIn(OPAQUE, summary)
        self.assertIn(REDACTED, summary)
        generic = hook._input_summary({"prompt": "SERVICE_ROLE_KEY=%s" % JWT}, "Task")
        self.assertNotIn(JWT, generic)
        self.assertIn(REDACTED, generic)


if __name__ == "__main__":
    unittest.main(verbosity=2)
