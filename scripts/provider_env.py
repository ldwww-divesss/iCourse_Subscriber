#!/usr/bin/env python3
"""Resolve configured provider secrets and launch Python without shell eval.

Usage: python scripts/provider_env.py -- python -u main.py
Only configured provider keys reach the child; SECRETS_CONTEXT is removed.
Without arguments, retain the legacy shell export format for compatibility.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import re
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.runtime.config import MODEL_PROVIDERS


def provider_environment(ctx: dict) -> dict[str, str]:
    # Secret names are case-insensitive on the GitHub side; normalise.
    lookup = {str(k).upper(): v for k, v in ctx.items() if v}

    resolved: dict[str, str] = {}
    for provider in MODEL_PROVIDERS:
        for field in ("api_key_env", "base_url_env"):
            name = provider.get(field)
            if not name or name in resolved:
                continue
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("Invalid provider environment variable name")
            value = lookup.get(name.upper())
            if value:
                resolved[name] = str(value)
    return resolved


def main():
    try:
        ctx = json.loads(os.environ.get("SECRETS_CONTEXT") or "{}")
        if not isinstance(ctx, dict):
            raise ValueError("SECRETS_CONTEXT must be a JSON object")
        resolved = provider_environment(ctx)
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    if len(sys.argv) > 1:
        if sys.argv[1] != "--" or len(sys.argv) < 3:
            print("Usage: provider_env.py -- COMMAND [ARGS...]", file=sys.stderr)
            return 1
        env = dict(os.environ)
        env.pop("SECRETS_CONTEXT", None)
        env.update(resolved)
        return subprocess.call(sys.argv[2:], env=env)
    for name, value in resolved.items():
        print(f"export {name}={shlex.quote(value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
