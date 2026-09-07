"""ajd — agent java debugger.

A generic CLI debugger that attaches to a remote JVM over JDWP
(-agentlib:jdwp=transport=dt_socket,server=y,suspend=n,address=*:15555)
and provides source-mapped breakpoints, stepping, and in-memory inspection.
"""

__version__ = "0.2.1"
