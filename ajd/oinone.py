"""oinone platform gql client — generic function invocation.

Mirrors the frontend's GenericFunctionService: a model (lower-camel gql
name) + a function name + JSON args + response fields, serialized as

    { <modelName>Query    { <functionName>(args) { fields } } }
    mutation { <modelName>Mutation { <functionName>(args) { fields } } }

against the platform endpoint (default path /pamirs/api).  Login keeps a
cookie jar on disk so later commands carry the session
(`pamirs_uc_session_id`).
"""

import http.cookiejar
import json
import os
import urllib.request

from .gql import send_gql

DEFAULT_COOKIE_JAR = os.path.expanduser("~/.ajd/cookies.txt")

LOGIN_MODEL = "pamirsUserTransient"
LOGIN_FUNCTION = "login"


class OinoneError(Exception):
    pass


# ---------------------------------------------------------- gql serialization

def _gql_literal(value):
    """JSON value -> gql literal (strings quoted, objects/arrays nested)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gql_literal(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_gql_literal(v)}" for k, v in value.items()) + "}"
    raise OinoneError(f"cannot serialize {value!r} as a gql literal")


def build_query(model, function, args=None, fields=None, mutation=False):
    """Assemble the gql text for one function invocation."""
    arg_text = ""
    if args:
        arg_text = "(" + ", ".join(f"{k}: {_gql_literal(v)}"
                                   for k, v in args.items()) + ")"
    field_text = ""
    if fields:
        names = [f.strip() for f in fields if f.strip()]
        field_text = "{ " + " ".join(names) + " }"
    verb = "mutation" if mutation else "query"
    # A plain `query` keyword is fine; the engine accepts the shorthand
    # form too, but the explicit keyword matches the frontend builder.
    return f"{verb} {{ {model}{'Mutation' if mutation else 'Query'} {{ {function}{arg_text} {field_text} }} }}"


# ---------------------------------------------------------------- cookie jar

def _jar(cookie_jar):
    jar = http.cookiejar.MozillaCookieJar(cookie_jar)
    if os.path.exists(cookie_jar):
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            pass  # corrupt jar — start fresh
    return jar


def _save_jar(jar, cookie_jar):
    os.makedirs(os.path.dirname(cookie_jar) or ".", exist_ok=True)
    jar.save(ignore_discard=True, ignore_expires=True)


def _request(url, query, variables=None, headers=None, cookie_jar=None,
             timeout=30.0):
    """POST the gql request, carrying cookies when a jar is configured."""
    import urllib.error
    jar = _jar(cookie_jar) if cookie_jar else None
    if jar is None:
        return send_gql(url, query, variables=variables, headers=headers,
                        timeout=timeout)
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    payload = json.dumps({"query": query,
                          **( {"variables": variables} if variables else {})}
                         ).encode("utf-8")
    request_headers = {"Content-Type": "application/json",
                       "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, data=payload, headers=request_headers,
                                 method="POST")
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise OinoneError(f"HTTP {exc.code} from {url}") from exc
    _save_jar(jar, cookie_jar)
    return json.loads(body) if body else None


# ------------------------------------------------------------ high-level ops

def login(url, login_name, password, pic_code=None, cookie_jar=None,
          headers=None, timeout=30.0):
    """Platform login mutation; returns the login result object."""
    user = {"login": login_name, "password": password}
    if pic_code:
        user["picCode"] = pic_code
    query = build_query(LOGIN_MODEL, LOGIN_FUNCTION, {"user": user},
                        fields=["errorCode", "errorMsg", "broken",
                                "errorField", "redirect { id }"],
                        mutation=True)
    response = _request(url, query, cookie_jar=cookie_jar, headers=headers,
                        timeout=timeout)
    return response


def exec_function(url, model, function, args=None, fields=None,
                  mutation=False, cookie_jar=None, headers=None,
                  timeout=30.0):
    """Generic function invocation; returns the parsed response."""
    query = build_query(model, function, args, fields, mutation=mutation)
    return _request(url, query, cookie_jar=cookie_jar, headers=headers,
                    timeout=timeout)


def count(url, model, rsql="1==1", cookie_jar=None, headers=None,
          timeout=30.0):
    """countByWrapper sugar — the least intrusive probe of a model."""
    query = build_query(model, "countByWrapper",
                        args={"queryWrapper": {"rsql": rsql}})
    return _request(url, query, cookie_jar=cookie_jar, headers=headers,
                    timeout=timeout)
