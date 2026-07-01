"""Shared runner input provenance signature.

The constant is intentionally build-stable, not per-call random. The OS
injected-input flag is the rigorous discriminator between physical operator
input and synthetic input; this signature identifies synthetic input emitted by
this runner. Against other synthetic injectors it is best-effort only because a
foreign process could replay the same value.
"""

from __future__ import annotations

RUNNER_INPUT_SIGNATURE = 0x4E434442
