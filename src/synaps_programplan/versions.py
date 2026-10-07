"""Package / upstream version pins (explicit, reproducible)."""

from typing import Literal

NAME = "SynAPS-ProgramPlan"
VERSION = "0.1.0"

# ISO 16290 TRL 4: lab fixtures, synthetic programs and automated checks.
# Not a GOST R 58048 UGT assessment and not a customer pilot.
ISO16290_TRL = 4
CLAIM_LEVEL: Literal["experiment"] = "experiment"

# SynAPS commit this SynAPS-ProgramPlan release is validated against (direct use, no fork).
# Bump deliberately together with pyproject.toml; never float on branch tips.
SYNAPS_COMMIT = "1feb50b33568cb86c1bfd12b9d7cd7247e17ee49"
SYNAPS_REPO = "https://github.com/KonkovDV/SynAPS"
