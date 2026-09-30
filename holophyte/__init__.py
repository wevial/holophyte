import sys as _sys

# Line-buffered, so progress on a piped stdout lands in order with stderr.
if hasattr(_sys.stdout, "reconfigure"):
    _sys.stdout.reconfigure(line_buffering=True)
del _sys
