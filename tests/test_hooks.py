from __future__ import annotations

import unittest
from pathlib import Path

from agent.hooks import HookManager


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hooks = HookManager(enabled=True, workspace=Path.cwd())

    def test_sensitive_file_is_denied(self) -> None:
        decision = self.hooks.before_tool("write", {"path": ".env"})
        self.assertFalse(decision.allowed)
        self.assertIn("环境变量", decision.reason)

    def test_rm_rf_is_denied_with_alternative(self) -> None:
        decision = self.hooks.before_tool("bash", {"command": "rm -rf build/"})
        self.assertFalse(decision.allowed)
        self.assertIn("git clean", decision.alternative)

    def test_force_push_is_denied_with_alternative(self) -> None:
        decision = self.hooks.before_tool(
            "bash", {"command": "git push --force origin main"}
        )
        self.assertFalse(decision.allowed)
        self.assertIn("revert", decision.alternative)

    def test_hooks_switch_disables_denial(self) -> None:
        hooks = HookManager(enabled=False, workspace=Path.cwd())
        decision = hooks.before_tool("bash", {"command": "rm -rf build/"})
        self.assertTrue(decision.allowed)
        self.assertEqual(hooks.denials, [])


if __name__ == "__main__":
    unittest.main()
