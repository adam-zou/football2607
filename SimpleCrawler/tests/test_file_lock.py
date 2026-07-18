import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ExclusiveFileLockTests(unittest.TestCase):
    def test_second_process_lock_fails_without_waiting(self) -> None:
        from simple_crawler.file_lock import ExclusiveFileLock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            environment = os.environ.copy()
            python_path = str(Path(__file__).resolve().parents[1])
            existing_python_path = environment.get("PYTHONPATH")
            if existing_python_path:
                python_path = os.pathsep.join((python_path, existing_python_path))
            environment["PYTHONPATH"] = python_path
            child_script = """
import sys
from pathlib import Path
from simple_crawler.file_lock import ExclusiveFileLock, FileAlreadyLocked

try:
    with ExclusiveFileLock(Path(sys.argv[1]), timeout=0):
        pass
except FileAlreadyLocked:
    raise SystemExit(23)
"""
            with ExclusiveFileLock(path, timeout=0):
                result = subprocess.run(
                    [sys.executable, "-c", child_script, str(path)],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            self.assertEqual(result.returncode, 23, result.stderr)

    def test_lock_can_be_reacquired_after_release(self) -> None:
        from simple_crawler.file_lock import ExclusiveFileLock

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.lock"
            with ExclusiveFileLock(path, timeout=0):
                pass
            with ExclusiveFileLock(path, timeout=0):
                pass


if __name__ == "__main__":
    unittest.main()
