"""Freedom Ridge synthetic dataset generator.

A dependency-light (Pillow-only) generator that draws real images whose pixels
match every Freedom Ridge typed-label field — for trying Freedom Ridge with fake
data and for integration-testing annotation filtering, rendering, patching, and
evaluation.

    from fridge.synth import generate, SyntheticSpec
"""

from .demo import build_demo
from .generator import (
    GeneratedDataset,
    GeneratedSample,
    SyntheticSpec,
    generate,
)

__all__ = ["generate", "SyntheticSpec", "GeneratedDataset", "GeneratedSample", "build_demo"]
