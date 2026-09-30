"""A tiny product catalog for the storefront."""

PRODUCTS = [{"id": n, "name": f"Product {n:03d}", "price_cents": 100 * n} for n in range(1, 58)]


def page_bounds(page, per_page):
    """Start and end index of a 1-based page."""
    start = (page - 1) * per_page
    return start, start + per_page


def search(products, term):
    """Products whose name contains `term`, ignoring case."""
    needle = term.lower()
    return [p for p in products if needle in p["name"].lower()]


def cheapest(products, count=5):
    """The `count` cheapest products, cheapest first."""
    return sorted(products, key=lambda p: p["price_cents"])[:count]


def _positive_int(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"expected an integer of at least 1, got {value!r}")


def paginate(items, page=1, per_page=10):
    _positive_int(page)
    _positive_int(per_page)
    items = list(items)
    total = len(items)
    start, end = page_bounds(page, per_page)
    return {
        "items": items[start:end],
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": -(-total // per_page),
        "has_prev": page > 1,
        "has_next": end < total,
    }
