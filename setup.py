"""Build configuration for the optional native writer extension.

Compile in place (from the repository root) with::

    python setup.py build_ext --inplace

The extension is optional: when it cannot be built, Flint falls back to
pure-Python buffered writes (see ``core/writer.write_stream``).
"""

from setuptools import Extension, setup

setup(
    name="flint-native",
    version="1.0.1",
    description="Optional native writer extension for Flint",
    ext_modules=[
        Extension(
            "core._native_writer",
            sources=["core/_native_writer.c"],
        )
    ],
)
