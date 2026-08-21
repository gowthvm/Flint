"""Build configuration for the optional native writer extension.

Compile in place (from the repository root) with::

    python setup.py build_ext --inplace

The extension is optional: when it cannot be built, Flint falls back to
pure-Python buffered writes (see ``core/writer.write_stream``).
"""

import os
import sys

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class optional_build_ext(build_ext):
    """Build extension that tolerates compiler failures."""

    def run(self):
        try:
            super().run()
        except Exception as e:
            print(
                f"WARNING: Failed to build native writer extension: {e}",
                file=sys.stderr,
            )
            print("WARNING: Falling back to pure-Python write path.", file=sys.stderr)

    def build_extension(self, ext):
        try:
            super().build_extension(ext)
        except Exception as e:
            print(
                f"WARNING: Failed to build '{ext.name}': {e}",
                file=sys.stderr,
            )
            print("WARNING: Falling back to pure-Python write path.", file=sys.stderr)


def _has_native_writer():
    """Check if the native writer extension was built."""
    return os.path.exists(
        os.path.join("core", "_native_writer.pyd")
    ) or os.path.exists(os.path.join("core", "_native_writer.so"))


ext_modules = []
if _has_native_writer() or "build_ext" in sys.argv:
    ext_modules.append(
        Extension(
            "core._native_writer",
            sources=["core/_native_writer.c"],
        )
    )

setup(
    name="flint-native",
    version="1.1.2",
    description="Optional native writer extension for Flint",
    ext_modules=ext_modules,
    cmdclass={"build_ext": optional_build_ext},
)
