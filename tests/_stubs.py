"""Offline import shims for bot.py.

The sandbox runs under INTEGRATIONS_ONLY: the third-party packages that bot.py
imports at module top level (requests, urllib3 adapters, python-dotenv,
notion-client) cannot be installed from a registry. The pure functions we want
to test (normalize_notice, build_slack_*, load_state/save_state, etc.) only rely
on the standard library, so we register minimal stand-in modules in sys.modules
*before* importing bot. This lets the test suite import and exercise the REAL
functions from bot.py without reimplementing them and without network access.

If the real dependencies are actually installed (e.g. in CI on Python 3.11 with
requirements.txt), the stubs are skipped and the genuine packages are used.
"""

from __future__ import annotations

import sys
import types


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def install_stub_dependencies() -> None:
    """Register minimal stubs for bot.py's third-party imports if missing."""

    # --- requests + requests.adapters ---
    try:
        import requests  # noqa: F401
    except ImportError:
        requests_mod = _ensure_module("requests")

        class Session:  # pragma: no cover - not exercised by offline tests
            def __init__(self) -> None:
                self.headers = {}

            def mount(self, *args, **kwargs) -> None:
                pass

            def post(self, *args, **kwargs):
                raise RuntimeError("network disabled in offline tests")

        class _RequestException(Exception):
            pass

        class _HTTPError(_RequestException):
            pass

        requests_mod.Session = Session
        requests_mod.RequestException = _RequestException
        requests_mod.HTTPError = _HTTPError

        adapters_mod = _ensure_module("requests.adapters")

        class HTTPAdapter:  # pragma: no cover
            def __init__(self, *args, **kwargs) -> None:
                pass

        adapters_mod.HTTPAdapter = HTTPAdapter
        requests_mod.adapters = adapters_mod

    # --- urllib3.util.retry.Retry ---
    try:
        from urllib3.util.retry import Retry  # noqa: F401
    except ImportError:
        util_mod = _ensure_module("urllib3.util")
        retry_mod = _ensure_module("urllib3.util.retry")

        class Retry:  # pragma: no cover
            def __init__(self, *args, **kwargs) -> None:
                pass

        retry_mod.Retry = Retry
        util_mod.retry = retry_mod

    # --- dotenv.load_dotenv ---
    try:
        from dotenv import load_dotenv  # noqa: F401
    except ImportError:
        dotenv_mod = _ensure_module("dotenv")

        def load_dotenv(*args, **kwargs):  # pragma: no cover
            return False

        dotenv_mod.load_dotenv = load_dotenv

    # --- notion_client.Client (bot.py already tolerates ImportError) ---
    # Left untouched: bot.py wraps this import in try/except and sets Client=None.
