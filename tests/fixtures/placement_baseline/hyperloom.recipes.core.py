# Frozen function sources from main 8539c28; executed with the current module globals.

def call(self, args, inputs=None):
        """JSON object on stdout of one ss-recipe call; exit 1 raises RecipeError with the core's message. Output is
        read through files, at most MAX_OUTPUT bytes each, as UTF-8."""
        inputs = inputs or {}
        with tempfile.TemporaryDirectory(prefix="hl-recipe-") as directory:
            for name, value in inputs.items():
                (Path(directory) / name).write_text(value if isinstance(value, str) else json.dumps(value),
                                                    encoding="utf-8")
            # Input names in args stand for the temporary files written above.
            argv = [self.executable, *[str(Path(directory) / a) if a in inputs else a for a in args]]
            with open(Path(directory) / ".stdout", "w+b") as stdout, open(Path(directory) / ".stderr", "w+b") as stderr:
                try:
                    out = self.run(argv, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=TIMEOUT_S,
                                   **no_window())
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise RecipeError(f"ss-recipe unavailable: {exc}") from exc
                stdout.seek(0)
                stderr.seek(0)
                text, err = _bounded(stdout, "output"), _bounded(stderr, "error output")
        message = err.strip()
        for prefix in (name + ": " for name in NAMES):
            if message.startswith(prefix):
                message = message[len(prefix):]
                break
        if out.returncode == 1:
            raise RecipeError(message or "ss-recipe refused")
        if out.returncode != 0:
            raise RecipeError(f"ss-recipe usage error: {message}")
        try:
            result = json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RecipeError(f"ss-recipe printed no JSON: {exc}") from exc
        if not isinstance(result, dict):
            raise RecipeError(f"ss-recipe printed {type(result).__name__}, not a JSON object")
        return result
