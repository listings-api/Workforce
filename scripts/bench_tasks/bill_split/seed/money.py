"""Money helpers. Amounts are whole cents."""


def share(total_cents, weight, weight_sum):
    """One participant's proportional share of `total_cents`."""
    return round(total_cents * weight / weight_sum)


def format_cents(cents):
    """Render cents as a dollar string, for example 1234 -> '$12.34'."""
    sign = "-" if cents < 0 else ""
    dollars, rest = divmod(abs(cents), 100)
    return f"{sign}${dollars}.{rest:02d}"
