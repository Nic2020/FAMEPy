# SPDX-License-Identifier: MIT
"""Compile our independent C fixture. No FAME headers or libraries are used."""

import os
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = root / "build" / "test-shim"
    out.mkdir(parents=True, exist_ok=True)
    source = root / "tests" / "native" / "shim.c"
    if os.name == "nt":
        compiler = shutil.which("cl")
        if compiler is None:
            raise SystemExit("Run in an x64 MSVC developer terminal (cl is required).")
        library = out / "famepy_test.dll"
        command = [compiler, "/nologo", "/LD", "/W4", str(source), "/link", f"/OUT:{library}"]
    else:
        compiler = shutil.which("cc")
        if compiler is None:
            raise SystemExit("A C compiler named cc is required.")
        library = out / "libfamepy_test.so"
        command = [
            compiler,
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-shared",
            "-fPIC",
            str(source),
            "-o",
            str(library),
        ]
    subprocess.run(command, cwd=out, check=True)
    print(library)


if __name__ == "__main__":
    main()
