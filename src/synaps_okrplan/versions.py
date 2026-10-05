"""Package / upstream version pins (explicit, reproducible)."""

from typing import Literal

OKRPLAN_VERSION = "0.1.0"

# ISO 16290 TRL 4: lab fixtures, synthetic programs and automated checks.
# Not a GOST R 58048 UGT assessment and not a customer pilot.
ISO16290_TRL = 4
CLAIM_LEVEL: Literal["experiment"] = "experiment"

# SynAPS commit this OKRPlan release is validated against (direct use, no fork).
# Bump deliberately together with pyproject.toml; never float on branch tips.
SYNAPS_COMMIT = "786cf1b3bc915941558e7e50b2935de684a32643"
SYNAPS_REPO = "https://github.com/KonkovDV/SynAPS"
