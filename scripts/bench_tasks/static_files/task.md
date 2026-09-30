The docs site needs a small static file server function. Create `static_server.py` with `serve(root, url_path)`, which returns a tuple `(status, content_type, body)`.

`root` is the folder that holds the site and `url_path` is what follows the host in a request URL (for example `/guide/intro.html?v=2`). It may be percent-encoded and may carry a query string or a fragment, which must be ignored.

- An existing file returns `200`, a content type guessed from its name (`application/octet-stream` when unknown) and its bytes.
- A folder returns its `index.html` the same way, with or without a trailing slash. A folder without one returns `404`.
- A missing file returns `404`.
- Anything that would end up outside `root` returns `403`.
- Errors return a short plain-text bytes body, and `serve` must never raise.

`http_utils.py` has some helpers you may find useful. Add tests for `serve` to the repo.
