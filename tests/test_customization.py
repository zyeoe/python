from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent.customization import load_agents_instructions


class AgentsInstructionTests(unittest.TestCase):
    def test_global_then_project_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_file = root / "home" / "AGENTS.md"
            project_file = root / "project" / "AGENTS.md"
            global_file.parent.mkdir(parents=True)
            project_file.parent.mkdir(parents=True)
            global_file.write_text("global-only\n", encoding="utf-8")
            project_file.write_text("project-only\n", encoding="utf-8")

            instructions = load_agents_instructions(
                project_file.parent,
                global_path=global_file,
                enabled=True,
            )

            self.assertEqual(instructions.layers, ["global", "project"])
            self.assertLess(
                instructions.prompt.index("global-only"),
                instructions.prompt.index("project-only"),
            )


if __name__ == "__main__":
    unittest.main()
