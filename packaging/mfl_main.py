"""PyInstaller entry point (ADR-104).

A thin launcher so the frozen app has a single, unambiguous script to run.
It just calls ``mfl_desktop.__main__.main`` (which uses absolute imports, so
it freezes cleanly). The build is driven by ``packaging/mfl.spec``.
"""
import sys

from mfl_desktop.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
