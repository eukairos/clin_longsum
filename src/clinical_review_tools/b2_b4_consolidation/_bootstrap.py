# clin_longsum - tiered longitudinal clinical summarization
# Copyright (C) 2026 Eukairos
# SPDX-License-Identifier: AGPL-3.0-only
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of version 3 of the GNU Affero General Public License as published
# by the Free Software Foundation. This program is distributed WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU Affero General Public License for details.

"""
_bootstrap.py
=============
Path bootstrap for scripts that live in a src/ SUBFOLDER (evaluation/, database/,
clinical_review_tools/) but import top-level modules (config, holdout,
retention_taxonomy, and the b*/c*/f* siblings) using flat `import config` style.

Running `python <script>.py` puts only the SCRIPT's own directory on sys.path[0],
so a subfolder script cannot see src/. Importing this module first inserts src/
(and the repo root) onto sys.path, after which the flat imports resolve. Idempotent
and dependency-free (stdlib only), so it is safe as the very first import.

Usage -- first line of every subfolder script, before any first-party import:

    import _bootstrap  # noqa: F401  (puts src/ on sys.path)
    import config
    ...

This copy sits two levels below src/, so it walks up one more directory than the
evaluation/ and database/ copies. Keep the resulting _SRC identical to theirs.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))                   # .../src/clinical_review_tools/b2_b4_consolidation
_SRC = os.path.dirname(os.path.dirname(_HERE))                       # .../src
_ROOT = os.path.dirname(_SRC)                                        # repo root
for _p in (_SRC, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
