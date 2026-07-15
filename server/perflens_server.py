#!/usr/bin/env python3
"""Compatibility shim — the server moved to the perflens package
(src/perflens/server.py). This wrapper keeps old invocations
(`python3 server/perflens_server.py ...`) working for one release.

Preferred:  pip install perflens && perflens serve ...
Dev runs:   PYTHONPATH=src python3 -m perflens.cli serve ...
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'src'))
from perflens.server import main  # noqa: E402

if __name__ == '__main__':
    main()
