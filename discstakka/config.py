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
    def __init__(self, data_dir=None, port=None, art_dir=None):
        self.data_dir = data_dir or os.environ.get("DISCSTAKKA_DATA") or DEFAULT_DATA
        self.port = int(port or os.environ.get("DISCSTAKKA_PORT", "5050"))
        self._art_dir = art_dir

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
        """Where cover art is written, and served from.

        Defaults beside the shipped static files, which is where
        docker-compose.yml mounts it. DISCSTAKKA_ART moves it, which an install
        whose code sits somewhere read-only - a nix store path, say - has to do.
        It is served by its own route rather than as a static file precisely so
        it does not have to live inside the package.
        """
        return (
            self._art_dir
            or os.environ.get("DISCSTAKKA_ART")
            or os.path.join(ROOT, "static", "art")
        )
