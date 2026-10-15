"""A developer's own .env must not change what the suite sees: the CLI reads an empty one here."""

import os
import tempfile

fd, path = tempfile.mkstemp(prefix="keel-test-", suffix=".env")
os.close(fd)
os.environ["KEEL_ENV_FILE"] = path
