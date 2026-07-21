"""Build script — compiles licensing + cloud_license into native C extensions for
distribution. Dev installs (``pip install -e .``) skip Cython and use the plain .py
sources; ``python -m build`` / ``pip install .`` (release) compile them via Cython."""

import os

from setuptools import Extension, setup
from setuptools.command.build_py import build_py as _build_py


SKIP_CYTHON = os.environ.get("ENGRAPHIS_SKIP_CYTHON", "").strip() == "1"
EXT_MODULES = []

if not SKIP_CYTHON:
    try:
        from Cython.Build import cythonize
    except ImportError:
        cythonize = None

    if cythonize is not None and not (
        # pip install -e . does NOT run build_ext, so skip. Detect via
        # SETUPTOOLS_ENABLE_FEATURES (setuptools >= 69) or legacy editable flag.
        os.environ.get("SETUPTOOLS_ENABLE_FEATURES", "") == "legacy-editable"
    ):
        EXT_MODULES = cythonize(
            [
                Extension(
                    "engraphis.licensing",
                    ["engraphis/licensing.py"],
                ),
                Extension(
                    "engraphis.cloud_license",
                    ["engraphis/cloud_license.py"],
                ),
            ],
            compiler_directives={
                "language_level": "3",
                "boundscheck": False,
                "wraparound": False,
            },
            # Defer the dep files into the build dir so the engraphis/ source tree
            # stays clean on every platform (Cython generates licensing.c etc.)
            build_dir="build",
        )


class build_py(_build_py):
    """Exclude licensing.py and cloud_license.py from the package when compiled
    extensions exist — ship the .pyd/.so instead so the compiled version wins."""

    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if not EXT_MODULES:
            return modules
        return [
            m for m in modules
            if (package, m[0]) not in {
                ("engraphis", "licensing"),
                ("engraphis", "cloud_license"),
            }
        ]


setup(
    ext_modules=EXT_MODULES,
    cmdclass={"build_py": build_py},
)
