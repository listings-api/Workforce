The storefront's product list page needs pagination. In `catalog.py`, add a function `paginate(items, page=1, per_page=10)`.

It returns a dict with these keys: `items` (the slice for that page, as a list), `page`, `per_page`, `total` (number of items overall), `total_pages`, `has_prev` and `has_next`.

Pages are numbered from 1. A `page` or `per_page` below 1, or one that is not an integer, raises `ValueError`. Asking for a page past the last one is not an error: it returns an empty `items` list.

Add tests for it to the repo's existing test file.
