"""Allow `python -m perflens.mcp` alongside `perflens mcp`."""

import sys

from perflens.mcp import main

if __name__ == '__main__':
    sys.exit(main())
