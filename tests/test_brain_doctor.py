import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import brain_doctor


class BrainDoctorEnvTests(unittest.TestCase):
    def test_check_env_warns_when_env_file_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(os.environ, {}, clear=True):
            previous = Path.cwd()
            os.chdir(tmpdir)
            try:
                result = brain_doctor._check_env()
            finally:
                os.chdir(previous)

        self.assertEqual(result.status, "warn")
        self.assertIn(".env is missing", result.detail)

    def test_check_env_ok_when_openai_api_key_is_set(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True
        ):
            previous = Path.cwd()
            os.chdir(tmpdir)
            try:
                result = brain_doctor._check_env()
            finally:
                os.chdir(previous)

        self.assertEqual(result.status, "ok")
        self.assertIn("Configured via", result.detail)


if __name__ == "__main__":
    unittest.main()
