"""Repository-wide pytest configuration.

Loads ``pytester`` so the testing package's pytest11 entry-point plugin can be
verified against isolated temporary projects.
"""

pytest_plugins = ["pytester"]
