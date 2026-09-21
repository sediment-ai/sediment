# SPDX-License-Identifier: AGPL-3.0-or-later
"""
``SEDIMENT_LABEL_CONFIDENCE_*`` env config for the label-confidence policy,
following ``verifier_commands.py``'s ``VerifierCommandsSettings`` shape: a ``pydantic-settings``
loader with an ``SEDIMENT_``-prefixed env, and a ``resolve()`` that hands
back the plain policy object the projections actually consume. Nothing
threads this automatically — a runner constructs ``LabelConfidenceSettings()`` and
passes ``.resolve()`` into ``resolve_confidence``/``project_dpo``/
``project_sft``, exactly as ``export_rlvr`` does for ``VerifierCommandsSettings``.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from sediment_derive import CIResolutionPolicy

from .label_confidence import LabelConfidencePolicy


class LabelConfidenceSettings(BaseSettings):
    """Loads the confidence-ladder knobs (``label_confidence.py``'s ``LabelConfidencePolicy``)
    from the environment, one ``SEDIMENT_LABEL_CONFIDENCE_<FIELD>`` variable per knob —
    e.g. ``SEDIMENT_LABEL_CONFIDENCE_CI_FAIL_MULTIPLIER=0.5``. Every field defaults to
    ``LabelConfidencePolicy``'s own default, so an unconfigured deployment resolves
    the exact same policy whether or not it has ever heard of this settings
    class (mirrors ``VerifierCommandsSettings``'s
    unconfigured-is-default-behavior convention).
    """

    # extra="ignore": the process env / .env is shared with other components
    # (apps/api's Settings, VerifierCommandsSettings), so
    # tolerate keys this model does not own instead of failing construction.
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="SEDIMENT_LABEL_CONFIDENCE_", extra="ignore"
    )

    explicit_accept_confidence: float = LabelConfidencePolicy.explicit_accept_confidence
    explicit_reject_confidence: float = LabelConfidencePolicy.explicit_reject_confidence
    baseline_confidence: float = LabelConfidencePolicy.baseline_confidence
    implicit_accept_multiplier: float = LabelConfidencePolicy.implicit_accept_multiplier
    implicit_reject_multiplier: float = LabelConfidencePolicy.implicit_reject_multiplier
    ci_pass_multiplier: float = LabelConfidencePolicy.ci_pass_multiplier
    ci_fail_multiplier: float = LabelConfidencePolicy.ci_fail_multiplier
    clean_reliability: float = CIResolutionPolicy.clean_reliability
    suspected_flake_reliability: float = CIResolutionPolicy.suspected_flake_reliability
    non_verdict_reliability: float = CIResolutionPolicy.non_verdict_reliability

    def resolve(self) -> LabelConfidencePolicy:
        """The loaded policy — validated by ``LabelConfidencePolicy.__post_init__``
        exactly as a directly-constructed policy would be, so a bad env
        value fails the same way a bad literal would."""
        # Every field above is a LabelConfidencePolicy knob of the same name, so the
        # dump maps straight across and the two can't drift apart field by
        # field. policy_version is not a knob and keeps its own default.
        values = self.model_dump()
        ci_resolution = CIResolutionPolicy(
            clean_reliability=values.pop("clean_reliability"),
            suspected_flake_reliability=values.pop("suspected_flake_reliability"),
            non_verdict_reliability=values.pop("non_verdict_reliability"),
        )
        return LabelConfidencePolicy(**values, ci_resolution=ci_resolution)
