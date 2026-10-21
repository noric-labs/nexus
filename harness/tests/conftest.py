import os
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1]
# The preload library lives in the sideload package repo next to this checkout.
CRASHTRACE_SRC = Path(os.environ.get(
    "NEXUS_CRASHTRACE_SRC",
    HARNESS.parents[1] / "torment-pkgs/packages/torment-crashtrace/torment_crashtrace.c"))


@pytest.fixture(scope="session")
def crashtrace_lib(tmp_path_factory):
    """libtorment_crashtrace.so compiled from the package source."""
    cc = shutil.which(os.environ.get("CC", "gcc")) or shutil.which("cc")
    if not cc or not CRASHTRACE_SRC.exists():
        pytest.skip(f"need a C compiler and {CRASHTRACE_SRC}")
    out = tmp_path_factory.mktemp("crashtrace") / "libtorment_crashtrace.so"
    subprocess.run([cc, "-O2", "-fPIC", "-shared", "-fvisibility=hidden", "-Wall", "-Wextra",
                    "-Werror", "-o", str(out), str(CRASHTRACE_SRC)], check=True)
    return str(out)
