from __future__ import annotations

import argparse
from collections.abc import Sequence

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beak", description="Beak browser-rendering crawler service.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server = subparsers.add_parser("server", help="Start the local FastAPI service.")
    server.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Bind host or IP. Use 0.0.0.0 or all for all IPv4 interfaces, "
            ":: or all-v6 for all IPv6 interfaces, or ::1 for IPv6 localhost."
        ),
    )
    server.add_argument("--port", default=8000, type=int, help="Bind port.")
    server.add_argument("--reload", action="store_true", help="Enable uvicorn reload for local development.")

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "server":
        run_server(host=args.host, port=args.port, reload=args.reload)
        return

    parser.error(f"unknown command: {args.command}")


def run_server(*, host: str, port: int, reload: bool = False) -> None:
    uvicorn.run("beak.main:app", host=normalize_bind_host(host), port=port, reload=reload)


def normalize_bind_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized in {"all", "*"}:
        return "0.0.0.0"
    if normalized in {"all-v6", "ipv6-all"}:
        return "::"
    return host
