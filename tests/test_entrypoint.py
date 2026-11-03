"""The serve entrypoint must bind the socket from config (issue #7).

Nothing in app/ used to bind a socket: config.host/config.port only fed
Config.api_base_url() and the /api/server/restart target, so setting
VIBE_HARNESS_PORT=9000 did not move the listener (the README starts uvicorn
with an explicit --port). app.main.serve() is the real entrypoint now —
`python -m app.main` or the `vibe-harness` console script — and it calls
uvicorn.run("app.main:app", host=config.host, port=config.port, ...), so the
env vars genuinely control the bind and api_base_url()/restart agree with it.
`uvicorn app.main:app` keeps working as-is.
"""
from __future__ import annotations

import runpy

import pytest
import uvicorn

from app.config import load_config


@pytest.fixture
def serve_calls(monkeypatch):
    """Mock uvicorn.run and return the list of calls it captured."""
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    return calls


def test_serve_binds_configured_host_and_port(monkeypatch, serve_calls):
    from app.main import serve

    monkeypatch.setenv("VIBE_HARNESS_HOST", "0.0.0.0")
    monkeypatch.setenv("VIBE_HARNESS_PORT", "9000")
    serve()
    assert len(serve_calls) == 1
    args, kwargs = serve_calls[0]
    assert args == ("app.main:app",)
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9000


def test_serve_defaults_to_localhost_8734(monkeypatch, serve_calls):
    from app.main import serve

    monkeypatch.delenv("VIBE_HARNESS_HOST", raising=False)
    monkeypatch.delenv("VIBE_HARNESS_PORT", raising=False)
    serve()
    args, kwargs = serve_calls[0]
    assert args == ("app.main:app",)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8734


def test_python_m_app_main_reaches_serve(monkeypatch, serve_calls):
    # `python -m app.main` must run the __main__ guard and reach serve().
    import sys

    monkeypatch.setenv("VIBE_HARNESS_PORT", "9101")
    # Drop the already-imported module so runpy executes a fresh __main__ copy.
    monkeypatch.delitem(sys.modules, "app.main", raising=False)
    runpy.run_module("app.main", run_name="__main__")
    assert serve_calls, "python -m app.main did not call uvicorn.run"
    assert serve_calls[0][1]["port"] == 9101


def test_bound_port_agrees_with_api_base_url(monkeypatch, serve_calls):
    # api_base_url() (the MCP server's target URL) and the restart relaunch
    # read the same config, so they must agree with the actually bound port.
    from app.main import serve

    monkeypatch.setenv("VIBE_HARNESS_PORT", "9000")
    serve()
    config = load_config()
    bound_port = serve_calls[0][1]["port"]
    assert config.port == bound_port == 9000
    assert config.api_base_url() == f"http://127.0.0.1:{bound_port}"


def test_load_config_reads_host_and_port_env():
    assert load_config({}).host == "127.0.0.1"
    assert load_config({}).port == 8734
    cfg = load_config({"VIBE_HARNESS_HOST": "0.0.0.0", "VIBE_HARNESS_PORT": "9000"})
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9000
    assert cfg.api_base_url() == "http://127.0.0.1:9000"
