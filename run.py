"""Точка входа: python run.py --provider fake --conditions all --n-tasks 30 --repeats 1"""

import sys

from mas_drift.runner.cli import main

if __name__ == "__main__":
    sys.exit(main())
