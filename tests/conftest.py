"""
Test harness.

The dashboard imports streamlit, altair and snowflake.snowpark at module load.
None of those are needed to exercise the reconciliation and projection logic,
and requiring them would mean a contributor could not run the tests without a
Snowflake account. They are stubbed here instead, so `pytest` works on a clean
checkout with nothing but pandas, numpy and pytest installed.
"""

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


class _Stub:
    """Accepts any attribute access, call, or context-manager use."""

    def __getattr__(self, name):
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __enter__(self):
        return _Stub()

    def __exit__(self, *exc):
        return False


def _make_streamlit() -> types.ModuleType:
    module = types.ModuleType("streamlit")

    def cache_data(*args, **kwargs):
        # Used both bare and as @st.cache_data(ttl=...); support both.
        if args and callable(args[0]):
            return args[0]

        def decorator(fn):
            return fn

        return decorator

    def columns(spec, *args, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [_Stub() for _ in range(count)]

    module.cache_data = cache_data
    module.columns = columns
    module.set_page_config = lambda *a, **k: None
    module.__getattr__ = lambda name: _Stub()
    return module


def _make_altair() -> types.ModuleType:
    module = types.ModuleType("altair")
    module.__getattr__ = lambda name: _Stub()
    return module


def _install_stubs() -> None:
    sys.modules.setdefault("streamlit", _make_streamlit())
    sys.modules.setdefault("altair", _make_altair())

    snowflake = sys.modules.setdefault("snowflake", types.ModuleType("snowflake"))
    snowpark = sys.modules.setdefault(
        "snowflake.snowpark", types.ModuleType("snowflake.snowpark")
    )
    context = types.ModuleType("snowflake.snowpark.context")

    def get_active_session():
        raise RuntimeError(
            "No Snowflake session in tests. Logic under test must be called "
            "with DataFrames supplied directly, not through the loaders."
        )

    context.get_active_session = get_active_session
    sys.modules["snowflake.snowpark.context"] = context
    snowpark.context = context
    snowflake.snowpark = snowpark


_install_stubs()
sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session")
def app():
    """The dashboard module, imported once with stubs in place."""
    import streamlit_app

    return streamlit_app
