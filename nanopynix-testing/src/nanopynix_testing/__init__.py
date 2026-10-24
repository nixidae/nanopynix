"""The beartype instrumentation that every suite loads first.

``beartype_hook`` installs beartype's import hook in the pytest process, and
``_subprocess_startup`` installs it in each interpreter the suite starts. Each
suite names the hook with ``-p`` in its own ``pytest.ini``.
"""

from __future__ import annotations
