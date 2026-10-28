# Frozen function sources from main 8539c28; executed with the current module globals.

def in_process(argv, loader):
    """Run a cheap read operation in process: the CLI's parser and the same commands.run, with cli.main's error
    handling (a refusal is exit 1 with the message; unknown id 3; conflict 4)."""
    from hyperloom import cli
    from hyperloom.recipes import commands, service
    args = cli.build_parser(RaisingParser).parse_args(argv)
    try:
        value, ok = commands.run(loader, args)
    except service.NotFound as exc:
        return Result(3, b"", f"hyperloom: {exc}".encode())
    except service.Conflict as exc:
        return Result(4, b"", f"hyperloom: {exc}".encode())
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        return Result(1, b"", f"hyperloom: {exc}".encode())
    except Exception as exc:          # the CLI exits 1 on an unexpected error too; the request still gets its answer
        return Result(1, b"", f"hyperloom: internal error ({type(exc).__name__}): {exc}".encode())
    return Result(0 if ok else 1, json.dumps(value, indent=2).encode("utf-8"), b"")
