"""Allow Postout to run with: python -m postout."""

from .cli import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
