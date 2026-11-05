import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_benchmark_module():
    path = ROOT / "scripts" / "benchmark_api_latency.py"
    spec = importlib.util.spec_from_file_location("benchmark_api_latency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class BenchmarkApiLatencyTests(unittest.TestCase):
    def test_percentile_interpolates_even_median(self):
        module = _load_benchmark_module()

        self.assertEqual(2.5, module._percentile([1, 2, 3, 4], 0.5))
        self.assertEqual(3.7, module._percentile([1, 2, 3, 4], 0.9))


if __name__ == "__main__":
    unittest.main()
