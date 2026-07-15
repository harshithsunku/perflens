"""PerfLens — real-time Linux performance profiler with a web UI.

A static C agent runs `perf` on the target device and streams compressed
samples over TCP; this package is the receiving server: parsing,
aggregation, source mapping, and the browser UI.
"""

__version__ = '0.6.0'
