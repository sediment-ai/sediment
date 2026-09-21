# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attribution-score calibration demonstration tests.

Reference values are hand-computed in comments before the assertions, not
derived from the implementation under test.
"""

from __future__ import annotations

import math

from sediment_export import brier_score, expected_calibration_error


def test_similarity_scores_apply_existing_calibration_metrics() -> None:
    pairs = [
        (0.92, True),
        (0.88, True),
        (0.76, True),
        (0.71, False),
        (0.64, True),
        (0.55, False),
        (0.43, False),
        (0.31, False),
    ]

    # Hand-computed Brier:
    #   true rows:
    #     (0.92 - 1)^2 + (0.88 - 1)^2 + (0.76 - 1)^2 + (0.64 - 1)^2
    #     = 0.0064 + 0.0144 + 0.0576 + 0.1296 = 0.208
    #   false rows:
    #     0.71^2 + 0.55^2 + 0.43^2 + 0.31^2
    #     = 0.5041 + 0.3025 + 0.1849 + 0.0961 = 1.0876
    #   mean: (0.208 + 1.0876) / 8 = 0.16195
    assert math.isclose(brier_score(pairs), 0.16195, rel_tol=1e-12)

    # Hand-computed ECE with calibration.py's default 10 equal-width bins:
    #   [0.3, 0.4): (1/8) * abs(0.00 - 0.31) = 0.03875
    #   [0.4, 0.5): (1/8) * abs(0.00 - 0.43) = 0.05375
    #   [0.5, 0.6): (1/8) * abs(0.00 - 0.55) = 0.06875
    #   [0.6, 0.7): (1/8) * abs(1.00 - 0.64) = 0.04500
    #   [0.7, 0.8): (2/8) * abs(0.50 - 0.735) = 0.05875
    #   [0.8, 0.9): (1/8) * abs(1.00 - 0.88) = 0.01500
    #   [0.9, 1.0]: (1/8) * abs(1.00 - 0.92) = 0.01000
    #   total = 0.29
    assert math.isclose(expected_calibration_error(pairs), 0.29, rel_tol=1e-12)
