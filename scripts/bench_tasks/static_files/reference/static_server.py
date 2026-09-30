import mimetypes
import os
from urllib.parse import unquote, urlsplit


def _error(status, message):
    return status, "text/plain", message.encode()


def serve(root, url_path):
    try:
        raw = urlsplit(url_path).path
        relative = unquote(raw)
        if "\x00" in relative:
            return _error(400, "bad request")
        real_root = os.path.realpath(root)
        target = os.path.realpath(os.path.join(real_root, relative.lstrip("/")))
        if os.path.commonpath([real_root, target]) != real_root:
            return _error(403, "forbidden")
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
            if os.path.commonpath([real_root, os.path.realpath(target)]) != real_root:
                return _error(403, "forbidden")
        if not os.path.isfile(target):
            return _error(404, "not found")
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as handle:
            return 200, content_type, handle.read()
    except (OSError, ValueError):
        return _error(404, "not found")
