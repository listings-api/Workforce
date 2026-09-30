"""Money helpers. Amounts are whole cents."""


def share(total_cents, weight, weight_sum):
    """One participant's proportional share of `total_cents`."""
    return round(total_cents * weight / weight_sum)


def format_cents(cents):
    """Render cents as a dollar string, for example 1234 -> '$12.34'."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars}.{rest:02d}"


def _whole(value, minimum, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}, got {value!r}")


def split_evenly(total_cents, n):
    _whole(total_cents, 0, "total_cents")
    _whole(n, 1, "n")
    base, extra = divmod(total_cents, n)
    return [base + (1 if index < extra else 0) for index in range(n)]


def split_weighted(total_cents, weights):
    _whole(total_cents, 0, "total_cents")
    weights = list(weights)
    if not weights:
        raise ValueError("weights must not be empty")
    for weight in weights:
        _whole(weight, 0, "weight")
    weight_sum = sum(weights)
    if weight_sum == 0:
        raise ValueError("at least one weight must be positive")
    shares = [total_cents * weight // weight_sum for weight in weights]
    remainders = [total_cents * weight % weight_sum for weight in weights]
    leftover = total_cents - sum(shares)
    order = sorted(range(len(weights)), key=lambda index: (-remainders[index], index))
    for index in order[:leftover]:
        shares[index] += 1
    return shares
