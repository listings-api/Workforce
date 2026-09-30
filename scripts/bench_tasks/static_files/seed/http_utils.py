"""Small helpers shared by the web handlers."""

import os


def safe_join(root, relative):
    """Join `relative` onto `root`, or return None when the result would leave `root`."""
    path = os.path.normpath(os.path.join(root, relative))
    if path.startswith(os.path.normpath(root)):
        return path
    return None


def status_line(code):
    """The HTTP status line for the handful of codes this project uses."""
    reasons = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}
    return f"HTTP/1.1 {code} {reasons[code]}"
