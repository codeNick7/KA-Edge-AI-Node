"""Standalone sanity check for an edge_node.export-generated module.

Loads the generated file directly (no package import, no sklearn/numpy
needed) and calls trained_predict once, to prove it runs on a bare Python
interpreter -- this is the check board_dry_run.sh runs on the board itself.

Usage: python3 check_exported_model.py /path/to/trained_dry_run.py
"""

import importlib.util
import sys


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: check_exported_model.py <path-to-exported-module.py>")
        sys.exit(1)

    path = sys.argv[1]
    spec = importlib.util.spec_from_file_location("exported_model", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    print("MODEL_VERSION:", module.MODEL_VERSION)
    label, confidence = module.trained_predict(0.15, 60.0, -1.0, -0.01)
    print("sample prediction:", label, confidence)


if __name__ == "__main__":
    main()
