"""A small module with the kind of boundary behaviour test suites routinely miss."""


def discount(subtotal, threshold=100.0, rate=0.1):
    """Apply `rate` off once the order reaches `threshold`."""
    applied = 0.0
    if subtotal >= threshold:
        applied = subtotal * rate
    total = subtotal - applied
    return round(total, 2)


def clamp(value, lo=0.0):
    """Floor `value` at `lo`."""
    if value < lo:
        return lo
    return value


def validate_rate(rate):
    """Reject a discount rate above the ceiling."""
    ceiling = 0.9
    if rate > ceiling:
        raise ValueError(f"{rate=} exceeds {ceiling=}")
    return rate


def total_items(counts):
    """Sum a sequence of item counts."""
    total = 0
    for count in counts:
        total = total + count
    return total
