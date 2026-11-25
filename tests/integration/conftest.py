"""Shared fixtures for GLSim integration tests.

GLSim keeps one VM clock for the whole server process: a transaction that
warps time (genvm_datetime) leaks into later tests. Reset the clock to a
fixed date before every test so deadlines behave deterministically.
"""

import json
import urllib.request

import pytest

# NOTE (toolchain mismatch, 2026-11-24): genlayer-py 0.19 builds method
# calldata as {"": method, "args": [...]} but the GLSim bundled with
# genlayer-test 0.30.0 rejects that with "No method in calldata" and only
# dispatches the legacy {"method": method, "args": [...]} shape (verified
# by direct gen_call probes). Patch the builder so integration tests can
# exercise the contract; the contract itself and the StudioNet deploy path
# (genlayer-js CLI) are unaffected.
def _patched_make_calldata_object(method=None, args=None, kwargs=None):
    ret = {}
    if method is not None:
        ret["method"] = method
    if args is not None and len(args) > 0:
        ret["args"] = args
    if kwargs is not None and isinstance(kwargs, dict) and kwargs:
        ret["kwargs"] = kwargs
    return ret


try:
    import genlayer_py.contracts.actions as _actions
    import genlayer_py.contracts.utils as _utils
    import genlayer_py.consensus.consensus_main.encoder as _encoder

    _actions.make_calldata_object = _patched_make_calldata_object
    _utils.make_calldata_object = _patched_make_calldata_object
    _encoder.make_calldata_object = _patched_make_calldata_object
except Exception:
    pass

GLSIM_URL = "http://127.0.0.1:4000/api"
BASE_TIME = "2026-11-22T00:00:00Z"


def _rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GLSIM_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"GLSim RPC {method} failed: {payload['error']}")
    return payload["result"]


@pytest.fixture(autouse=True)
def _reset_sim_time():
    _rpc("sim_setTime", {"datetime": BASE_TIME})
    yield
    _rpc("sim_setTime", {"datetime": BASE_TIME})
