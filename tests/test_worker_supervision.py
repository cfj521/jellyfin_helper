"""后台 worker 线程存活测试。

覆盖两个回归点：
1. run_forever 领取任务时 DB 短暂不可用，不应让 dispatch-pipeline 线程退出；
2. scheduler._spawn_thread 的兜底重启，任何 worker 未捕获异常都不应永久消失。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.dispatch import pipeline_worker, scheduler


class WorkerSupervisionTest(unittest.TestCase):
    def test_pipeline_survives_claim_db_error(self):
        stop = threading.Event()
        pipeline = pipeline_worker.DispatchPipeline(stop_event=stop)
        calls = []

        def claim():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("db temporarily unavailable")
            stop.set()
            pipeline_worker.trigger.set()
            return None

        with patch.object(pipeline_worker, "CLAIM_RETRY_SECONDS", 0.01), \
                patch.object(pipeline, "_claim_next_pending", claim):
            pipeline.run_forever()

        self.assertEqual(len(calls), 2)

    def test_spawn_thread_restarts_after_exception(self):
        stop = threading.Event()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("worker crashed")
            stop.set()

        with patch.object(scheduler, "WORKER_RESTART_SECONDS", 0.01):
            scheduler._spawn_thread(flaky, "test-worker", stop_event=stop)
            self.assertTrue(stop.wait(2))

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
