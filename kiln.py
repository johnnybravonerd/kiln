#!/usr/bin/env python3
"""Kiln compiler entry point.  Usage: python3 kiln.py <command> [file.kn] ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if sys.version_info < (3, 8):
    sys.exit("Kiln needs Python 3.8 or newer")

from kiln_lang.driver import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
