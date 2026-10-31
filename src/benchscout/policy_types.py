"""Effect classes, shared by the policy and the action registry."""

from enum import IntEnum


class Effect(IntEnum):
    SAFE = 0  # auto-run
    DISRUPTIVE = 1  # changes device/instrument state; needs approval
    DANGEROUS = 2  # can damage hardware or destroy state; approval + warning
