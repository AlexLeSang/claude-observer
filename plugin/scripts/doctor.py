#!/usr/bin/env python3
"""Launcher for the usage-observer doctor: `python3 -B doctor.py [--capture PATH] [--json]`.

Kept minimal on purpose: it parses under old Pythons, checks the interpreter version, then imports
the real module. A syntax error or a missing sibling in the real module is reported here, not as a traceback, so the
doctor still answers the user who most needs it (an old Python, a damaged install).
"""

import sys

if sys.version_info < (3, 10):  # noqa: UP036 - the guard is the point of this file
    version = sys.version_info
    sys.stderr.write(f"Python 3.10 or newer is required; found {version[0]}.{version[1]}.{version[2]}\n")
    sys.exit(2)

try:
    import doctor_checks
except Exception as error:  # any failure at import: the doctor must still answer, with the type name only
    sys.stderr.write(
        f"doctor: could not load the checks ({type(error).__name__}); the plugin files may be damaged or incomplete\n"
    )
    sys.exit(1)

sys.exit(doctor_checks.main())
