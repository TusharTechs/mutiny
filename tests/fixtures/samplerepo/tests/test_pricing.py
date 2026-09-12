"""The existing suite: good coverage, no boundary cases. Every line executes."""
import pytest

from pricing import clamp, discount, validate_rate


def test_small_order_gets_no_discount():
    assert discount(50.0) == 50.0


def test_large_order_gets_discount():
    assert discount(150.0) == 135.0


def test_clamp_floors_negatives():
    assert clamp(-5.0) == 0.0


def test_clamp_passes_positives():
    assert clamp(7.5) == 7.5


def test_rate_above_ceiling_rejected():
    with pytest.raises(ValueError):
        validate_rate(0.95)


def test_rate_below_ceiling_allowed():
    assert validate_rate(0.5) == 0.5
