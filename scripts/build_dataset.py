"""Build/resume the dataset and export it: python scripts/build_dataset.py [--target 1000]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signalpost.cli import main  # noqa: E402

if __name__ == "__main__":
    code = main(["build-dataset", *sys.argv[1:]])
    main(["export"])
    sys.exit(code)
