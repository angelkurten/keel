"""A developer's own .env must not change what the suite sees: the CLI reads an empty one here."""

import os
import tempfile

fd, path = tempfile.mkstemp(prefix="keel-test-", suffix=".env")
os.close(fd)
os.environ["KEEL_ENV_FILE"] = path
for _k in ("KEEL_TENANT", "KEEL_AGENT", "KEEL_DB", "KEEL_INDEX", "KEEL_EMBEDDER"):
    os.environ.pop(_k, None)  # nor do the variables of a developer's shell
