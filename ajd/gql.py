"""GraphQL request helper — used to drive a debugged app into code paths
(e.g. fire an action/function call while a breakpoint waits for it)."""

import json
import urllib.request


def send_gql(url, query, variables=None, headers=None, timeout=30.0):
    """POST a GraphQL query; returns the parsed JSON response.

    Raises urllib.error.URLError / HTTPError on transport or HTTP errors.
    """
    payload = {"query": query}
    if variables is not None:
        payload["variables"] = variables
    request_headers = {"Content-Type": "application/json",
                       "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "replace")
    if not body:
        return None
    return json.loads(body)
