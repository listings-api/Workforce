"""A tiny product catalog for the storefront."""

PRODUCTS = [{"id": n, "name": f"Product {n:03d}", "price_cents": 100 * n} for n in range(1, 58)]


def page_bounds(page, per_page):
    """Start and end index of a 1-based page."""
    start = page * per_page
    return start, start + per_page


def search(products, term):
    """Products whose name contains `term`, ignoring case."""
    needle = term.lower()
    return [p for p in products if needle in p["name"].lower()]


def cheapest(products, count=5):
    """The `count` cheapest products, cheapest first."""
    return sorted(products, key=lambda p: p["price_cents"])[:count]
