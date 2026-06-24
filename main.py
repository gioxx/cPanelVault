import argparse
import logging
import sys

__version__ = "2.1.0"


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def cmd_backup(args: argparse.Namespace) -> None:
    from backup.config import load_config, load_notifications
    from backup.runner import run_backup

    cfg = load_config(args.config)
    notifications = load_notifications(args.config)

    if args.all:
        targets = list(cfg.keys())
    elif args.host:
        targets = [args.host]
    else:
        print("Specify a host or use --all", file=sys.stderr)
        sys.exit(1)

    failed = False
    for name in targets:
        if name not in cfg:
            print(f"Host '{name}' not found in {args.config}", file=sys.stderr)
            sys.exit(1)
        result = run_backup(cfg[name], notifications)
        if result["status"] != "success":
            failed = True

    sys.exit(1 if failed else 0)


def cmd_clean(args: argparse.Namespace) -> None:
    from backup.cleaner import clean_old_backups
    from backup.config import load_config

    cfg = load_config(args.config)
    targets = list(cfg.keys()) if args.all else [args.host]

    for name in targets:
        if name not in cfg:
            print(f"Host '{name}' not found", file=sys.stderr)
            sys.exit(1)
        host_cfg = cfg[name]
        removed = clean_old_backups(host_cfg.destination_folder, host_cfg.retention_days, dry_run=args.dry_run)
        print(f"[{name}] {'(dry-run) ' if args.dry_run else ''}{len(removed)} file(s) removed.")


def cmd_serve(args: argparse.Namespace) -> None:
    import os
    os.environ.setdefault("CONFIG_FILE", args.config)
    import uvicorn
    uvicorn.run("web.app:app", host=args.host, port=args.port, reload=False)


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(prog="hostingbackup", description="cPanel backup manager")
    parser.add_argument("--config", default="ftp_config.json", metavar="FILE", help="Config JSON (default: ftp_config.json)")

    sub = parser.add_subparsers(dest="command", required=True)

    # backup
    p_backup = sub.add_parser("backup", help="Run backup for one or all hosts")
    group = p_backup.add_mutually_exclusive_group(required=True)
    group.add_argument("host", nargs="?", help="Credential set name from config")
    group.add_argument("--all", action="store_true", help="Run backup for all configured hosts")
    p_backup.set_defaults(func=cmd_backup)

    # clean
    p_clean = sub.add_parser("clean", help="Remove expired local backups")
    grp2 = p_clean.add_mutually_exclusive_group(required=True)
    grp2.add_argument("host", nargs="?")
    grp2.add_argument("--all", action="store_true")
    p_clean.add_argument("--dry-run", action="store_true", help="Show what would be deleted without removing anything")
    p_clean.set_defaults(func=cmd_clean)

    # serve
    p_serve = sub.add_parser("serve", help="Start the web UI and scheduler")
    p_serve.add_argument("--host", default="0.0.0.0")
    p_serve.add_argument("--port", type=int, default=8080)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args()
    logging.getLogger(__name__).info("HostingBackup v%s starting (%s)", __version__, args.command)
    args.func(args)


if __name__ == "__main__":
    main()
