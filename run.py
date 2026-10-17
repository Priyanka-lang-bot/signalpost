"""Zero-install entry point: python run.py company 923609016  (needs: pip install -r requirements.txt)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from signalpost.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
