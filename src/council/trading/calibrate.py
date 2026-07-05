"""Probability calibration helpers — pure math, no I/O.

extremize() counters documented LLM underconfidence (KalshiBench: models say
50% when the truth is ~80%) by scaling in logit space. alpha=1 is identity;
alpha>1 pushes estimates away from 0.5. Fitted later from journal buckets.
"""
from __future__ import annotations

import math

_EPS = 1e-6


def extremize(p: float, alpha: float = 1.3) -> float:
    p = min(max(p, _EPS), 1.0 - _EPS)
    logit = math.log(p / (1.0 - p)) * alpha
    return 1.0 / (1.0 + math.exp(-logit))


def brier(p: float, outcome: int) -> float:
    """Squared error of probability p against outcome (1=YES, 0=NO). Lower is better."""
    return round((p - float(outcome)) ** 2, 10)
