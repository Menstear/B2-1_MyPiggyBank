"""벤치마크가 결과를 검증하고 실제 data와 작업 폴더를 건드리지 않는지 확인한다."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from benchmarks.benchmark import CASES, main

ROOT = Path(__file__).resolve().parents[1]


class BenchmarkTest(unittest.TestCase):
    def test_all_cases_in_isolated_workers_with_memory_and_no_local_data_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "data").mkdir()
            sentinel = base / "data" / "transactions.jsonl"
            sentinel.write_text("실제 데이터는 건드리면 안 됨", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT)
            proc = subprocess.run(
                [sys.executable, "-m", "benchmarks.benchmark", "--rows", "40", "--repeat", "1",
                 "--memory", "--chunk-size", "3", "--fan-in", "2", "--io-buffer-size", "4096"],
                cwd=base, env=env, capture_output=True, text=True, check=True,
            )
            report = json.loads(proc.stdout)
            self.assertEqual([item["case"] for item in report["results"]], list(CASES))
            self.assertEqual(report["configuration"], {
                "rows": 40, "repeat": 1, "chunk_size": 3, "fan_in": 2, "io_buffer_size": 4096,
            })
            for item in report["results"]:
                self.assertGreaterEqual(item["median_seconds"], 0)
                self.assertIsNotNone(item["python_peak_mib"])
                self.assertEqual(len(item["samples"]), 1)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "실제 데이터는 건드리면 안 됨")
            self.assertEqual(set(base.rglob("*")), {base / "data", sentinel})

    def test_output_report_is_saved_at_requested_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "reports" / "run.json"
            self.assertEqual(main(["--rows", "3", "--repeat", "1", "--cases", "import",
                                   "--output", str(output)]), 0)
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["results"][0]["result_records"], 3)

    def test_invalid_sizes_fail_before_creating_data(self) -> None:
        for option, value in (("--rows", "0"), ("--repeat", "-1"), ("--chunk-size", "0"),
                              ("--fan-in", "1"), ("--io-buffer-size", "0")):
            with self.subTest(option=option), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main([option, value])
                self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
