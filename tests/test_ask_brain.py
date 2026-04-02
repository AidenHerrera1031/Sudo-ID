import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import ask_brain


class AskBrainMainTests(unittest.TestCase):
    def run_main(self, argv):
        stream = io.StringIO()
        with patch.object(sys, "argv", ["brain ask"] + argv), redirect_stdout(stream):
            ask_brain.main()
        return stream.getvalue().strip()

    def test_literal_lookup_falls_back_to_project_code_hits(self):
        weak_docs = ["File: CHANGELOG.md\nSummary: Updated file."]
        weak_metas = [{"source": "CHANGELOG.md", "kind": "file_summary"}]
        weak_dists = [1.7959]
        fallback_docs = ["Watcher smoke marker: glacier-lantern-4821\nSudo-ID watcher smoke test"]
        fallback_metas = [{"source": "WATCHER_SMOKE_TEST.md", "kind": "code_or_docs"}]
        fallback_dists = [1.7327]

        with patch.object(ask_brain, "retrieve_context", return_value=(weak_docs, weak_metas, weak_dists)), patch.object(
            ask_brain,
            "find_project_code_fallback",
            return_value=(fallback_docs, fallback_metas, fallback_dists),
        ) as fallback_mock, patch.object(ask_brain, "summarize_with_openai", return_value=""):
            output = self.run_main(["glacier-lantern-4821"])

        self.assertIn("glacier-lantern-4821", output)
        self.assertIn("Sudo-ID watcher smoke test", output)
        fallback_mock.assert_called_once_with("glacier-lantern-4821", ask_brain.DEFAULT_RESULTS)

    def test_non_literal_lookup_does_not_use_project_code_fallback(self):
        docs = ["Project Overview: Terminal-first project memory sidecar."]
        metas = [{"source": "project:overview", "kind": "project_identity"}]
        dists = [0.2]

        with patch.object(ask_brain, "retrieve_context", return_value=(docs, metas, dists)), patch.object(
            ask_brain,
            "find_project_code_fallback",
            return_value=([], [], []),
        ) as fallback_mock, patch.object(ask_brain, "summarize_with_openai", return_value=""):
            output = self.run_main(["What", "does", "this", "project", "do?"])

        self.assertIn("Terminal-first project memory sidecar", output)
        fallback_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
