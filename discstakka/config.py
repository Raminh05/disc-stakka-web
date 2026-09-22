"""Where the application keeps its state.

One object rather than three paths derived from __file__, so a test or the
simulator runner can point the whole application at a scratch directory without
going near the real catalogue.
"""

import os

# The project root, which is this package's parent: the app runs either from a
# checkout or from /app in the container, and in both the state sits beside the
# package rather than inside it.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA = os.path.join(ROOT, "data")


class Config(object):
    def __init__(self, data_dir=None, port=None):
        self.data_dir = data_dir or os.environ.get("DISCSTAKKA_DATA") or DEFAULT_DATA
        self.port = int(port or os.environ.get("DISCSTAKKA_PORT", "5050"))

    @property
    def db_path(self):
        return os.path.join(self.data_dir, "catalog.db")

    @property
    def trace_dir(self):
        return os.path.join(self.data_dir, "traces")

    @property
    def secret_key_path(self):
        return os.path.join(self.data_dir, "secret_key")

    @property
    def art_dir(self):
        """Cover art, which lives under static/ rather than in the data dir.

        Flask serves it from there and docker-compose.yml mounts it there, so it
        cannot follow DISCSTAKKA_DATA without the browser losing every image.
        """
        return os.path.join(ROOT, "static", "art")
