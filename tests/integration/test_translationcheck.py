"""Integration tests for TranslationCheck against GLSim (localnet).

Full consensus: leader + validators run the assessment with mocked
web/LLM responses (via transaction_context), so no real LLM API key is
needed.

Requires a running simulator: glsim --port 4000 --validators 5
Run: gltest tests/integration/ -v -s --network localnet
"""

import json
import urllib.request

from gltest import create_accounts, get_contract_factory
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

SOURCE_TEXT = "The quick brown fox jumps over the lazy dog."
SOURCE_LANG = "English"
TARGET_LANG = "French"
TRANSLATION_TEXT = "Le rapide renard brun saute par-dessus le chien paresseux."
REFERENCE_URL = "https://example.com/translations/reference-001"
REFERENCE_TEXT = "Reference translation: Le renard brun rapide saute par-dessus le chien paresseux."
GLSIM_URL = "http://127.0.0.1:4000/api"


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


def _assessment_context(verdict, accuracy, fluency, completeness):
    """Mocks for assessment: reference page + LLM verdict/scores."""
    return {
        "validators": [
            {
                "plugin_config": {
                    "mock_web_response": {
                        "nondet_web_request": {
                            REFERENCE_URL: {
                                "status": 200,
                                "body": "Reference page: Le renard brun rapide saute par-dessus le chien paresseux.",
                            },
                        }
                    },
                    "mock_response": {
                        "response": {
                            # NOTE (genlayer-test 0.30): the mock layer
                            # auto-parses a JSON-string mock into a dict,
                            # but exec_prompt(response_format="json")
                            # expects the LLM to return JSON *text*, so the
                            # payload is double-encoded here (see direct
                            # tests for the same workaround).
                            ".*assessing a translation.*": json.dumps(
                                json.dumps(
                                    {"verdict": verdict,
                                     "accuracy": accuracy,
                                     "fluency": fluency,
                                     "completeness": completeness,
                                     "rationale": "Assessment matches the reference."}
                                )
                            )
                        }
                    },
                }
            }
        ],
    }


def _deploy():
    factory = get_contract_factory("TranslationCheck")
    contract = factory.deploy(args=[])
    alice, bob = create_accounts(2)
    return contract, alice, bob


def _register(contract, reporter):
    receipt = contract.connect(reporter).register_job(
        args=[SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, TRANSLATION_TEXT,
              REFERENCE_URL, REFERENCE_TEXT]
    ).transact()
    assert tx_execution_succeeded(receipt)
    return "job_0"


def test_full_flow_register_assess_needs_revision():
    contract, alice, bob = _deploy()
    job_id = _register(contract, alice)

    job = contract.get_job(args=[job_id]).call()
    assert job["status"] == "OPEN"
    assert job["reference_url"] == REFERENCE_URL
    assert job["source_lang"] == SOURCE_LANG
    assert job["target_lang"] == TARGET_LANG
    assert contract.get_job_count(args=[]).call() == 1

    # Assess with mocked reference + LLM verdict (full consensus)
    receipt = contract.assess(args=[job_id]).transact(
        transaction_context=_assessment_context("needs_revision", 75, 60, 70)
    )
    assert tx_execution_succeeded(receipt)
    job = contract.get_job(args=[job_id]).call()
    assert job["status"] == "ASSESSED"
    assert job["verdict"] == "needs_revision"
    assert job["accuracy"] == 75
    assert job["fluency"] == 60
    assert job["completeness"] == 70
    assert job["total_score"] == 205
    assert job["rationale"] == "Assessment matches the reference."


def test_full_flow_assess_accurate():
    contract, alice, bob = _deploy()
    job_id = _register(contract, alice)

    receipt = contract.assess(args=[job_id]).transact(
        transaction_context=_assessment_context("accurate", 95, 92, 90)
    )
    assert tx_execution_succeeded(receipt)
    job = contract.get_job(args=[job_id]).call()
    assert job["status"] == "ASSESSED"
    assert job["verdict"] == "accurate"
    assert job["total_score"] == 277


def test_close_job_owner_only_and_assess_closed_fails():
    contract, alice, bob = _deploy()
    job_id = _register(contract, alice)

    # Submitter (alice) is not the contract owner - close must fail
    receipt = contract.connect(alice).close_job(args=[job_id]).transact()
    assert tx_execution_failed(receipt)
    assert contract.get_job(args=[job_id]).call()["status"] == "OPEN"

    # Owner (default account) closes the job
    receipt = contract.close_job(args=[job_id]).transact()
    assert tx_execution_succeeded(receipt)
    assert contract.get_job(args=[job_id]).call()["status"] == "CLOSED"

    # A closed job cannot be assessed
    receipt = contract.assess(args=[job_id]).transact(
        transaction_context=_assessment_context("needs_revision", 75, 60, 70)
    )
    assert tx_execution_failed(receipt)


def test_assess_open_only():
    contract, alice, bob = _deploy()

    # Assessing a job that was never registered fails
    receipt = contract.assess(args=["job_99"]).transact(
        transaction_context=_assessment_context("needs_revision", 75, 60, 70)
    )
    assert tx_execution_failed(receipt)
