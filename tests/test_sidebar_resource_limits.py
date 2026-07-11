from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.personal_wechat_bot.control import sidebar_api


class SidebarResourceLimitsTest(unittest.TestCase):
    def test_resource_limits_use_the_shared_gpu_gate(self) -> None:
        data_dir = Path("isolated-data").resolve()
        state = {"scheduler": {"resource_pools": {}}}
        key_pool = mock.Mock()
        key_pool.concurrency_limit.return_value = 3
        gpu_snapshot = {
            "max_parallel": 1,
            "active_slots": 0,
            "policy": "test",
        }

        with mock.patch.object(sidebar_api, "_key_pool", return_value=key_pool), mock.patch.object(
            sidebar_api,
            "gpu_gate_snapshot",
            return_value=gpu_snapshot,
        ) as snapshot, mock.patch.object(
            sidebar_api,
            "_last_resource_audit",
            return_value={},
        ), mock.patch.object(
            sidebar_api,
            "_resource_scheduler_snapshot",
            return_value={},
        ):
            sidebar_api._inject_runtime_resource_limits(state, data_dir)

        snapshot.assert_called_once_with()
        pools = state["scheduler"]["resource_pools"]
        self.assertEqual(pools["llm"]["max_parallel"], 3)
        self.assertEqual(pools["gpu"]["max_parallel"], 1)
        self.assertEqual(pools["gpu"]["policy"], "test")

    def test_runtime_probe_uses_the_shared_gpu_gate(self) -> None:
        data_dir = Path("probe-data").resolve()
        config = SimpleNamespace(ocr_mode="cpu", asr_mode="cpu")

        with mock.patch.object(sidebar_api, "ensure_config", return_value=config), mock.patch.object(
            sidebar_api,
            "build_default_ocr_engine",
            side_effect=RuntimeError("disabled for test"),
        ) as ocr_factory, mock.patch.object(
            sidebar_api,
            "LocalAsrSubprocessEngine",
            side_effect=RuntimeError("disabled for test"),
        ) as asr_factory, mock.patch.object(
            sidebar_api,
            "gpu_gate_snapshot",
            return_value={"active_slots": 0},
        ) as snapshot:
            result = sidebar_api.sidebar_runtime_probe(data_dir)

        snapshot.assert_called_once_with()
        ocr_factory.assert_called_once_with(mode="cpu")
        asr_factory.assert_called_once_with(mode="cpu")
        self.assertEqual(result["gpu_gate"], {"active_slots": 0})


if __name__ == "__main__":
    unittest.main()
