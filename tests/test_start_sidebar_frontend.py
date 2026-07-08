from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import scripts.start_sidebar_frontend as starter


class StartSidebarFrontendTest(unittest.TestCase):
    def test_weflow_start_lock_prevents_duplicate_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            weflow_dir = root / "WeFlow"
            (weflow_dir / "node_modules").mkdir(parents=True)
            (weflow_dir / "package.json").write_text("{}", encoding="utf-8")
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"pid": 7777, "updated_at_epoch": time.time()}), encoding="utf-8")

            with mock.patch.object(starter, "WEFLOW_DIR", weflow_dir), mock.patch.object(
                starter.shutil, "which", return_value="npm.cmd"
            ), mock.patch.object(starter, "weflow_token", return_value="token"), mock.patch.object(
                starter, "weflow_health", return_value={"status": "error", "message": "not ready"}
            ), mock.patch.object(
                starter, "_pid_exists", return_value=True
            ), mock.patch.object(
                starter.subprocess, "Popen"
            ) as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_not_called()
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["state"], "start_in_progress")
            self.assertEqual(result["pid"], 7777)

    def test_stale_weflow_start_lock_is_replaced_by_single_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            weflow_dir = root / "WeFlow"
            (weflow_dir / "node_modules").mkdir(parents=True)
            (weflow_dir / "package.json").write_text("{}", encoding="utf-8")
            lock_path = data_dir / "runtime" / "weflow_start.lock"
            lock_path.parent.mkdir(parents=True)
            lock_path.write_text(json.dumps({"pid": 7777, "updated_at_epoch": time.time() - 999}), encoding="utf-8")
            process = mock.Mock(pid=8888)
            process.poll.return_value = None

            with mock.patch.object(starter, "WEFLOW_DIR", weflow_dir), mock.patch.object(
                starter.shutil, "which", return_value="npm.cmd"
            ), mock.patch.object(starter, "weflow_token", return_value="token"), mock.patch.object(
                starter, "weflow_health", return_value={"status": "error", "message": "not ready"}
            ), mock.patch.object(
                starter, "_pid_exists", return_value=False
            ), mock.patch.object(
                starter.subprocess, "Popen", return_value=process
            ) as popen:
                result = starter.ensure_weflow_started(
                    data_dir=data_dir,
                    host="127.0.0.1",
                    port=5031,
                    install_deps="auto",
                    wait_seconds=1,
                    required=False,
                    hidden=True,
                )

            popen.assert_called_once()
            self.assertEqual(result["status"], "starting")
            self.assertEqual(result["state"], "launched_waiting_for_health")
            self.assertEqual(result["pid"], 8888)
            self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
