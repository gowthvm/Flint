"""Build configuration for the optional native writer extension.

Compile in place (from the repository root) with::

    python setup.py build_ext --inplace

The extension is optional: when it cannot be built, Flint falls back to
pure-Python buffered writes (see ``core/writer.write_stream``).
"""

import re
import sys
from pathlib import Path

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

_version_file = Path(__file__).parent / "core" / "version.py"
APP_VERSION = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _version_file.read_text()).group(1)


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


setup(
    name="flint-native",
    version=APP_VERSION,
    description="Optional native writer extension for Flint",
    ext_modules=[
        Extension(
            "core._native_writer",
            sources=["core/_native_writer.c"],
        )
    ],
    cmdclass={"build_ext": optional_build_ext},
)
