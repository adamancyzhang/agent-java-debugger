"""Local source tree access: locate source files, show line windows, and
derive package hints from file paths for breakpoint resolution."""

import os

# Directories inside a module whose following path is the Java package.
_PACKAGE_MARKERS = ("src/main/java", "src/test/java", "src/main/kotlin",
                    "src/test/kotlin")


class SourceMap:
    def __init__(self, source_dirs):
        self.dirs = [os.path.abspath(d) for d in source_dirs]
        self._find_cache = {}

    # ------------------------------------------------------------- lookup

    def find_file(self, filename_or_path):
        """Locate a source file across the configured source dirs.

        Accepts either a bare file name or a (possibly partial) path.
        An exact basename match wins (`UserService.java` must not resolve
        to `NotUserService.java`); a suffix match is only the fallback.
        Returns an absolute path or None.
        """
        key = filename_or_path
        if key in self._find_cache:
            return self._find_cache[key]
        result = None
        fallback = None
        want_base = os.path.basename(filename_or_path)
        for root in self.dirs:
            for dirpath, dirnames, filenames in os.walk(root):
                # Skip build output and VCS noise.
                dirnames[:] = [d for d in dirnames
                               if d not in (".git", "node_modules", "target",
                                            "build", "dist", ".idea")]
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    if not full.endswith(filename_or_path):
                        continue
                    if os.path.basename(full) == want_base:
                        result = full
                        break
                    if fallback is None:
                        fallback = full
                if result:
                    break
            if result:
                break
        if result is None:
            result = fallback
        self._find_cache[key] = result
        return result

    def basename(self, path):
        return os.path.basename(path)

    def package_hint(self, path):
        """'…/src/main/java/com/foo/Bar.java' -> 'com.foo', else None."""
        norm = path.replace("\\", "/")
        for marker in _PACKAGE_MARKERS:
            idx = norm.find(marker)
            if idx >= 0:
                rest = norm[idx + len(marker):].lstrip("/")
                parts = rest.split("/")[:-1]
                return ".".join(parts) if parts else None
        return None

    # ------------------------------------------------------------ display

    def snippet(self, path, line, before=3, after=8):
        """Return (matched_line_number, [(lineno, text), ...]) around a line.

        lineno is None when the file could not be found or read.
        """
        located = self.find_file(path)
        if located is None:
            return None, None
        try:
            with open(located, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return None, None
        lo = max(1, line - before)
        hi = min(len(lines), line + after)
        return line, [(n, lines[n - 1].rstrip("\n")) for n in range(lo, hi + 1)]
