"""임시 데이터와 독립 프로세스로 시간/RSS를 측정한다.

실행: python3 -m benchmarks.benchmark --rows 100000 --repeat 3
메모리 추적은 --memory로 별도 실행하여 일반 시간 측정과 분리한다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gc
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any

from budget_app import ordering
from budget_app.cli import build_service
from budget_app.service import CSV_HEADER, SearchQuery

CASES = ("list", "search_limited", "search_all", "summary", "export", "import",
         "add", "update", "delete")


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("0보다 큰 정수를 지정하세요.")
    return number


def _fixture(directory: Path, rows: int) -> int:
    """추가 순서와 날짜 순서가 다른 동일한 입력을 매번 생성한다."""
    month_count = 0
    start = dt.date(2024, 1, 1)
    with (directory / "transactions.jsonl").open("w", encoding="utf-8") as jsonl, \
         (directory / "input.csv").open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(CSV_HEADER)
        for number in range(1, rows + 1):
            date = (start + dt.timedelta(days=number * 37 % 366)).isoformat()
            income = number % 5 == 0
            tx = {
                "id": f"TX-{number:06d}", "type": "income" if income else "expense",
                "date": date, "amount": (number % 100 + 1) * 100,
                "category": "salary" if income else ("food", "transport", "rent", "etc")[number % 4],
                "memo": f"벤치마크 거래 {number}: 한글, 쉼표와 태그",
                "tags": ["benchmark", "income" if income else "expense"],
            }
            jsonl.write(json.dumps(tx, ensure_ascii=False) + "\n")
            writer.writerow([tx[key] if key != "tags" else ",".join(tx[key]) for key in CSV_HEADER])
            month_count += date.startswith("2024-03")
    return month_count


def _rss_mib() -> float | None:
    try:
        import resource  # macOS/Linux. Windows에서도 시간/별도 tracemalloc은 사용 가능.
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    scale = 1024 ** 2 if sys.platform == "darwin" else 1024
    return peak / scale


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    if args.chunk_size is not None:
        ordering._CHUNK_SIZE = args.chunk_size
    if args.fan_in is not None:
        ordering._MERGE_FAN_IN = args.fan_in
    if args.io_buffer_size is not None:
        ordering._IO_BUFFER_SIZE = args.io_buffer_size
    with tempfile.TemporaryDirectory(prefix="budget-benchmark-case-") as temporary:
        base = Path(temporary)
        svc = build_service(str(base))
        if args.case != "import":
            shutil.copyfile(args.dataset / "transactions.jsonl", svc.tx_repo.path)
        gc.collect()
        if args.trace_memory:
            tracemalloc.start()
        start = time.perf_counter()
        if args.case == "list":
            result_count = len(svc.list_transactions(20))
        elif args.case == "search_limited":
            result_count = sum(1 for _ in svc.search(SearchQuery(), limit=20))
        elif args.case == "search_all":
            result_count = 0
            previous: tuple[str, int] | None = None
            for tx in svc.search(SearchQuery()):
                key = (tx.date, int(tx.id[3:]))
                if previous is not None and key > previous:
                    raise AssertionError("전체 검색의 날짜/ID 정렬이 잘못되었습니다.")
                previous = key
                result_count += 1
        elif args.case == "summary":
            result_count = svc.summary("2024-03")["count"]
        elif args.case == "export":
            result_count = svc.export_csv(base / "out.csv", SearchQuery())
        elif args.case == "import":
            result_count, skipped = svc.import_csv(args.dataset / "input.csv")
            if skipped:
                raise AssertionError("정상 벤치마크 입력을 건너뛰었습니다.")
        elif args.case == "add":
            tx = svc.add_transaction(type="expense", date="2024-12-31", amount=1234, category="food")
            if tx.id != f"TX-{args.rows + 1:06d}":
                raise AssertionError("추가 거래 ID가 잘못되었습니다.")
            result_count = 1
        elif args.case == "update":
            svc.update_transaction(f"TX-{args.rows:06d}", {"amount": 1234})
            result_count = 1
        else:
            svc.delete_transaction(f"TX-{args.rows:06d}")
            result_count = 1
        elapsed = time.perf_counter() - start
        python_peak = None
        if args.trace_memory:
            _, peak = tracemalloc.get_traced_memory()
            python_peak = peak / 1024 ** 2
            tracemalloc.stop()
        expected = (min(args.rows, 20) if args.case in ("list", "search_limited")
                    else args.month_count if args.case == "summary"
                    else 1 if args.case in ("add", "update", "delete") else args.rows)
        if result_count != expected:
            raise AssertionError(f"{args.case}: 결과 {result_count}, 기대 {expected}")
        return {"seconds": elapsed, "result_records": result_count,
                "peak_rss_mib": _rss_mib(), "python_peak_mib": python_peak}


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """준비 비용/프로세스 시작/터미널 출력을 제외하고 유스케이스를 측정한다."""
    results = []
    with tempfile.TemporaryDirectory(prefix="budget-benchmark-fixture-") as temporary:
        dataset = Path(temporary)
        month_count = _fixture(dataset, args.rows)
        for case in args.cases:
            command = [sys.executable, "-m", "benchmarks.benchmark", "--worker",
                       "--dataset", str(dataset), "--case", case, "--rows", str(args.rows),
                       "--month-count", str(month_count)]
            for option, value in (("--chunk-size", args.chunk_size), ("--fan-in", args.fan_in),
                                  ("--io-buffer-size", args.io_buffer_size)):
                if value is not None:
                    command.extend([option, str(value)])

            def trial(trace: bool = False) -> dict[str, Any]:
                proc = subprocess.run(command + (["--trace-memory"] if trace else []),
                                      cwd=Path(__file__).resolve().parents[1],
                                      capture_output=True, text=True, check=True)
                return json.loads(proc.stdout)

            samples = [trial() for _ in range(args.repeat)]
            times = [sample["seconds"] for sample in samples]
            rss = [sample["peak_rss_mib"] for sample in samples if sample["peak_rss_mib"] is not None]
            python_peak = trial(True)["python_peak_mib"] if args.memory else None
            results.append({"case": case, "result_records": samples[0]["result_records"],
                            "median_seconds": statistics.median(times),
                            "min_seconds": min(times), "max_seconds": max(times),
                            "max_peak_rss_mib": max(rss) if rss else None,
                            "python_peak_mib": python_peak, "samples": samples})
    return {
        "schema_version": 1,
        "environment": {"python": platform.python_version(), "system": platform.system(),
                        "release": platform.release(), "machine": platform.machine(),
                        "cpu_count": os.cpu_count()},
        "configuration": {"rows": args.rows, "repeat": args.repeat,
                          "chunk_size": args.chunk_size or ordering._CHUNK_SIZE,
                          "fan_in": args.fan_in or ordering._MERGE_FAN_IN,
                          "io_buffer_size": args.io_buffer_size or getattr(ordering, "_IO_BUFFER_SIZE", -1)},
        "measurement": "time excludes setup/startup/CLI printing; RSS includes process setup; tracemalloc is a separate trial",
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="임시 100k 가계부의 시간/RSS/메모리 측정")
    parser.add_argument("--rows", type=_positive, default=100000)
    parser.add_argument("--repeat", type=_positive, default=3)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=list(CASES))
    parser.add_argument("--chunk-size", type=_positive)
    parser.add_argument("--fan-in", type=_positive)
    parser.add_argument("--io-buffer-size", type=_positive)
    parser.add_argument("--memory", action="store_true", help="tracemalloc을 별도 프로세스에서 실행")
    parser.add_argument("--output", type=Path, help="JSON 결과 파일 (생략 시 stdout)")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dataset", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--case", choices=CASES, help=argparse.SUPPRESS)
    parser.add_argument("--month-count", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--trace-memory", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.fan_in is not None and args.fan_in < 2:
        parser.error("--fan-in은 2 이상이어야 합니다.")
    report = _worker(args) if args.worker else run_benchmark(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
