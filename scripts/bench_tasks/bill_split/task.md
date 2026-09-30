The expense-sharing feature needs to split a bill between people without losing or inventing a cent. In `money.py`, add two functions. Amounts are whole cents (integers).

`split_evenly(total_cents, n)` returns a list of `n` integer shares that add up to exactly `total_cents`. The shares differ by at most one cent, and any extra cents go to the earliest people in the list.

`split_weighted(total_cents, weights)` splits `total_cents` in proportion to `weights` (a list of non-negative integers) and returns a list of integer shares, one per weight, that add up to exactly `total_cents`. Use the largest-remainder method: give everyone the whole cents of their exact share, then hand out the leftover cents one each to the largest fractional remainders, earlier people first when remainders are equal. A weight of 0 gets 0 cents.

Both raise `ValueError` for a negative or non-integer `total_cents`, for `n < 1` or an empty `weights`, and `split_weighted` also raises it for a negative weight or when every weight is 0.

Add tests for both to the repo's existing test file.
