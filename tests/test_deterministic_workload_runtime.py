import csv
import importlib.util
import json
from pathlib import Path
import py_compile

ROOT = Path("environment/workloads")
RUNNER = ROOT / "common" / "locust_runner.py"
DETERMINISTIC = ROOT / "common" / "deterministic.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deterministic = load_module("deterministic_workload", DETERMINISTIC)


def load_runner():
    return load_module("locust_runner", RUNNER)


def test_exact_percent_schedule_and_ids_are_reproducible():
    first = deterministic.exact_percent_schedule(123, (("a", 70), ("b", 30)))
    second = deterministic.exact_percent_schedule(123, (("a", 70), ("b", 30)))

    assert first == second
    assert first.count("a") == 70
    assert first.count("b") == 30
    assert deterministic.deterministic_uuid(123, 1, 2, "user") == deterministic.deterministic_uuid(123, 1, 2, "user")


def test_locust_profiles_compile_without_importing_runtime_dependencies(tmp_path):
    for path in (ROOT / "sock-shop" / "locustfile.py", ROOT / "otel-demo" / "locustfile.py", RUNNER):
        py_compile.compile(str(path), cfile=str(tmp_path / (path.name + ".pyc")), doraise=True)


def test_locust_runner_evaluates_95_percent_entry_slo(tmp_path, monkeypatch):
    runner = load_runner()
    prefix = tmp_path / "baseline"
    stats = Path(str(prefix) + "_stats.csv")
    with stats.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Type", "Name", "Request Count", "Failure Count", "Requests/s", "95%"])
        writer.writeheader()
        writer.writerow({"Type": "", "Name": "Aggregated", "Request Count": "100", "Failure Count": "5", "Requests/s": "10", "95%": "900"})
    env = {
        "RESBENCH_APPLICATION": "sock-shop",
        "RESBENCH_RUN_ID": "run-1",
        "RESBENCH_RANDOM_SEED": "123",
        "RESBENCH_TRAFFIC_MIX_JSON": json.dumps([{"flow": "browse", "weightPercent": 100}]),
        "RESBENCH_MINIMUM_SUCCESS_RATE": "0.95",
        "RESBENCH_MAXIMUM_ERROR_RATE": "0.05",
        "RESBENCH_MAXIMUM_P95_LATENCY_MS": "1500",
        "RESBENCH_MINIMUM_SAMPLES": "100",
        "RESBENCH_MINIMUM_THROUGHPUT_RATIO": "0.95",
        "RESBENCH_BASELINE_THROUGHPUT_RPS": "10",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    summary, qualified = runner.evaluate(stats)

    assert qualified is True
    assert summary["successRate"] == 0.95
    assert summary["errorRate"] == 0.05
    assert summary["checks"] == {
        "minimumSamples": True,
        "minimumSuccessRate": True,
        "maximumErrorRate": True,
        "maximumP95LatencyMs": True,
        "minimumThroughput": True,
    }
