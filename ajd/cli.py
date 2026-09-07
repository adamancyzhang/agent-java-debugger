"""ajd command-line entry: attach to a remote JVM, script it, or fire gql."""

import argparse
import json
import socket
import subprocess
import sys
import time

from . import __version__
from .oinone import DEFAULT_COOKIE_JAR
from .session import DebuggerSession


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="ajd",
        description="agent-java-debugger: a JDWP CLI debugger for remote JVMs "
                    "(-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,"
                    "address=*:15555)")
    parser.add_argument("--version", action="version",
                        version=f"ajd {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    attach = sub.add_parser("attach", help="attach to a running JVM's JDWP port")
    attach.add_argument("--host", default="127.0.0.1")
    attach.add_argument("--port", type=int, default=15555)
    attach.add_argument("--source-dir", action="append", default=[],
                        help="source tree(s) for file breakpoints and 'source' "
                             "(repeatable)")
    attach.add_argument("--exec", action="append", default=[],
                        help="run a command line instead of the REPL "
                             "(repeatable; e.g. --exec \"bp add ...\" "
                             "--exec continue)")
    attach.add_argument("--json", action="store_true",
                        help="emit machine-readable JSON lines (agent mode)")
    attach.add_argument("--timeout", type=float, default=60.0,
                        help="seconds to wait for the next stop (default 60)")
    attach.add_argument("--cmd-timeout", type=float, default=15.0,
                        help="per-command reply timeout (default 15)")
    attach.add_argument("--resume-after", type=float, default=60.0,
                        help="auto-resume a held stop after N seconds of "
                             "inactivity, so request threads are never "
                             "blocked by a forgotten breakpoint "
                             "(default 60; 0 disables)")

    gql = sub.add_parser("gql", help="POST a GraphQL request (e2e trigger)")
    gql.add_argument("--url", required=True)
    gql.add_argument("--query", required=True, help="GraphQL query text")
    gql.add_argument("--vars", dest="variables", help="variables as JSON")
    gql.add_argument("--headers", help="extra headers as JSON")
    gql.add_argument("--timeout", type=float, default=30.0)

    oinone = sub.add_parser("oinone",
                            help="gql requests against an oinone platform "
                                 "backend (login/exec/count)")
    oinone_subs = oinone.add_subparsers(dest="oinone_command", required=True)

    def _oinone_common(p):
        p.add_argument("--url", required=True,
                       help="backend base URL (gql endpoint, e.g. "
                            "http://host:8091/pamirs/api)")
        p.add_argument("--cookie-jar", default=None,
                       help=f"cookie jar file (default {DEFAULT_COOKIE_JAR})")
        p.add_argument("--headers", help="extra headers as JSON")
        p.add_argument("--timeout", type=float, default=30.0)

    ologin = oinone_subs.add_parser("login", help="log in and store the session cookie")
    _oinone_common(ologin)
    ologin.add_argument("--login", dest="login_name", default="admin")
    ologin.add_argument("--password", default="admin")
    ologin.add_argument("--pic-code", default=None, help="graphical captcha, if required")

    oexec = oinone_subs.add_parser("exec", help="invoke any model function "
                                                "(mirrors the frontend GenericFunctionService)")
    _oinone_common(oexec)
    oexec.add_argument("--model", required=True,
                       help="model gql name (lower-camel), e.g. action, pamirsUserTransient")
    oexec.add_argument("--function", required=True,
                       help="function name, e.g. countByWrapper, queryPage, login")
    oexec.add_argument("--args", default=None,
                       help="function arguments as a JSON object, e.g. "
                            "'{\"user\": {\"login\": \"admin\"}}'")
    oexec.add_argument("--fields", nargs="*", default=None,
                       help="response fields (space separated), e.g. id name")
    oexec.add_argument("--mutation", action="store_true",
                       help="issue a mutation instead of a query")

    ocount = oinone_subs.add_parser("count", help="countByWrapper sugar: probe a model")
    _oinone_common(ocount)
    ocount.add_argument("--model", required=True,
                        help="model gql name (lower-camel)")
    ocount.add_argument("--rsql", default="1==1", help="rsql filter (default 1==1)")

    skills = sub.add_parser("skills", help="list/view installed skills "
                                           "(from skill-data/)")
    skills.add_argument("skill", nargs="*", default=None,
                        help="skill name to view ('skills oinone' or "
                             "'skills get oinone'); omit to list")
    skills.add_argument("--json", action="store_true",
                        help="machine-readable listing")

    run = sub.add_parser("run", help="launch a local JVM with JDWP enabled and "
                                     "attach to it")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=15555)
    run.add_argument("--source-dir", action="append", default=[])
    run.add_argument("--exec", action="append", default=[])
    run.add_argument("--json", action="store_true")
    run.add_argument("--timeout", type=float, default=60.0)
    run.add_argument("--cmd-timeout", type=float, default=15.0)
    run.add_argument("--resume-after", type=float, default=60.0)
    run.add_argument("--suspend", action="store_true",
                     help="suspend=y: the JVM waits for the debugger before "
                          "running main")
    run.add_argument("java_args", nargs=argparse.REMAINDER,
                     help="arguments after '--' passed to java")
    return parser


def cmd_attach(args):
    session = DebuggerSession(args.host, args.port, source_dirs=args.source_dir,
                              cmd_timeout=args.cmd_timeout,
                              wait_timeout=args.timeout)
    try:
        session.attach()
    except (OSError, TimeoutError) as exc:
        print(f"ajd: cannot attach to {args.host}:{args.port}: {exc}",
              file=sys.stderr)
        return 2
    from .repl import Runner, run_commands
    runner = Runner(session, json_mode=args.json,
                    resume_after=args.resume_after)
    interrupted = False
    try:
        if args.exec:
            runner.announce()
            run_commands(runner, args.exec)
        else:
            runner.repl()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        runner.close()
        session.close()
    if interrupted:
        return 130  # 128 + SIGINT — scripts can distinguish Ctrl-C
    if runner.failures:
        # agent mode relies on the exit code: any command error or wait
        # timeout makes the script fail instead of reporting success
        return 1
    return 0


def cmd_gql(args):
    from .gql import send_gql
    try:
        variables = json.loads(args.variables) if args.variables else None
        headers = json.loads(args.headers) if args.headers else None
    except ValueError as exc:
        print(f"ajd: invalid JSON in --vars/--headers: {exc}",
              file=sys.stderr)
        return 2
    try:
        response = send_gql(args.url, args.query, variables=variables,
                            headers=headers, timeout=args.timeout)
    except Exception as exc:  # urllib errors — report and exit non-zero
        print(f"ajd: gql request failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0


def cmd_oinone(args):
    from . import oinone
    try:
        headers = json.loads(args.headers) if getattr(args, "headers", None) else None
    except ValueError as exc:
        print(f"ajd: invalid JSON in --headers: {exc}", file=sys.stderr)
        return 2
    cookie_jar = args.cookie_jar or oinone.DEFAULT_COOKIE_JAR
    try:
        if args.oinone_command == "login":
            response = oinone.login(args.url, args.login_name, args.password,
                                    pic_code=args.pic_code,
                                    cookie_jar=cookie_jar, headers=headers,
                                    timeout=args.timeout)
            code = oinone.login_error_code(response)
            if code not in (None, 0):
                print(f"ajd: login failed (errorCode={code})",
                      file=sys.stderr)
                return 1
        elif args.oinone_command == "exec":
            fn_args = json.loads(args.args) if args.args else None
            if fn_args is not None and not isinstance(fn_args, dict):
                raise ValueError("--args must be a JSON object")
            response = oinone.exec_function(
                args.url, args.model, args.function, args=fn_args,
                fields=args.fields, mutation=args.mutation,
                cookie_jar=cookie_jar, headers=headers, timeout=args.timeout)
        else:  # count
            response = oinone.count(args.url, args.model, rsql=args.rsql,
                                    cookie_jar=cookie_jar, headers=headers,
                                    timeout=args.timeout)
    except Exception as exc:
        print(f"ajd: oinone request failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(response, indent=2, ensure_ascii=False))
    return 0


def cmd_skills(args):
    from . import skills
    names = list(args.skill or [])
    if names and names[0] == "get":
        names = names[1:]
    if names:
        name = names[0]
        text = skills.read_skill(name)
        if text is None:
            print(f"ajd: no skill {name!r} — run 'ajd skills' to list",
                  file=sys.stderr)
            return 2
        print(text)
        return 0
    entries = skills.list_skills()
    if args.json:
        print(json.dumps({"skills": [{"name": n, "description": d}
                                     for n, d in entries]},
                         ensure_ascii=False))
    else:
        for name, desc in entries:
            print(f"{name:<28} {desc}")
    return 0


def _wait_for_port(host, port, deadline):
    """Poll until something accepts a JDWP handshake on host:port."""
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0) as s:
                s.sendall(b"JDWP-Handshake")
                if s.recv(14) == b"JDWP-Handshake":
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


def cmd_run(args):
    java_args = args.java_args
    if java_args and java_args[0] == "--":
        java_args = java_args[1:]
    if not java_args:
        print("ajd: run needs java arguments after '--' "
              "(e.g. ajd run -- -cp . MyApp)", file=sys.stderr)
        return 2
    agent = (f"-agentlib:jdwp=transport=dt_socket,server=y,"
             f"suspend={'y' if args.suspend else 'n'},"
             f"address={args.host}:{args.port}")
    proc = subprocess.Popen(["java", agent] + java_args)
    if not _wait_for_port(args.host, args.port, time.time() + 60):
        print("ajd: target JVM did not open its JDWP port in time",
              file=sys.stderr)
        proc.terminate()
        return 2
    code = cmd_attach(args)
    if proc.poll() is None:
        # Leave the app running after detach — debugging must not kill it.
        print(f"ajd: detached; target JVM (pid {proc.pid}) keeps running",
              file=sys.stderr)
    return code


def main(argv=None):
    # Windows consoles/redirects may use a legacy codepage (cp936/cp1252)
    # that cannot encode the Unicode markers (■ ◉ ▸ ✎) — force UTF-8 with
    # replacement instead of crashing with UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = _build_parser().parse_args(argv)
    if args.command == "attach":
        return cmd_attach(args)
    if args.command == "gql":
        return cmd_gql(args)
    if args.command == "oinone":
        return cmd_oinone(args)
    if args.command == "skills":
        return cmd_skills(args)
    if args.command == "run":
        return cmd_run(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
