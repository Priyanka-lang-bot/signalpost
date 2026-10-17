"""Run the benchmark: python scripts/benchmark.py [--sample 50]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signalpost.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["benchmark", *sys.argv[1:]]))
