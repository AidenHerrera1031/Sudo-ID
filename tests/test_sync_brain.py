import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sync_brain


def _write_jsonl(path: Path, records) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _session_records(session_id: str, cwd: str, text: str):
    return [
        {
            "timestamp": "2026-04-02T00:00:00.000Z",
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": cwd},
        },
        {
            "timestamp": "2026-04-02T00:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    ]


class SyncBrainChatScopingTests(unittest.TestCase):
    def test_parse_codex_sessions_only_keeps_current_project_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_root = root / "project-a"
            other_root = root / "project-b"
            sessions_dir = root / "sessions"

            _write_jsonl(
                sessions_dir / "2026/04/02/rollout-2026-04-02T00-00-00-11111111-1111-1111-1111-111111111111.jsonl",
                _session_records(
                    "11111111-1111-1111-1111-111111111111",
                    str(project_root),
                    "project scoped message",
                ),
            )
            _write_jsonl(
                sessions_dir / "2026/04/02/rollout-2026-04-02T00-00-00-22222222-2222-2222-2222-222222222222.jsonl",
                _session_records(
                    "22222222-2222-2222-2222-222222222222",
                    str(other_root),
                    "other project message",
                ),
            )

            entries, session_ids = sync_brain.parse_codex_sessions(
                sessions_dir,
                max_entries=100,
                max_session_files=10,
                project_root=project_root,
            )

        self.assertEqual(session_ids, {"11111111-1111-1111-1111-111111111111"})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["session_id"], "11111111-1111-1111-1111-111111111111")
        self.assertIn("project scoped message", entries[0]["text"])

    def test_history_fallback_stays_empty_when_no_project_sessions_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project_root = root / "project-a"
            other_root = root / "project-b"
            sessions_dir = root / "sessions"
            history_file = root / "history.jsonl"

            _write_jsonl(
                sessions_dir / "2026/04/02/rollout-2026-04-02T00-00-00-33333333-3333-3333-3333-333333333333.jsonl",
                _session_records(
                    "33333333-3333-3333-3333-333333333333",
                    str(other_root),
                    "other project session",
                ),
            )
            _write_jsonl(
                history_file,
                [
                    {
                        "session_id": "33333333-3333-3333-3333-333333333333",
                        "ts": 123,
                        "text": "history from another project",
                    }
                ],
            )

            with patch.object(sync_brain, "CODEX_SESSIONS_DIR", sessions_dir), patch.object(
                sync_brain, "CHAT_HISTORY_FILE", history_file
            ), patch.object(sync_brain, "CHAT_SOURCE", "history"), patch.object(
                sync_brain, "CHAT_PROJECT_ONLY", True
            ):
                source, entries = sync_brain.load_chat_entries(project_root)

        self.assertEqual(source, "history")
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
