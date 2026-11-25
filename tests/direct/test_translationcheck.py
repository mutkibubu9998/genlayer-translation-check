"""Direct-mode tests for TranslationCheck.

Fast, in-memory (~30ms). Leader function only - validator logic is NOT
exercised here (see tests/integration for full consensus). All web/LLM
calls are mocked.
"""

import json

SOURCE_TEXT = "The quick brown fox jumps over the lazy dog."
SOURCE_LANG = "English"
TARGET_LANG = "French"
TRANSLATION_TEXT = "Le rapide renard brun saute par-dessus le chien paresseux."
REFERENCE_URL = "https://example.com/translations/reference-001"
REFERENCE_TEXT = "Reference translation: Le renard brun rapide saute par-dessus le chien paresseux."


def _hex(addr) -> str:
    """Raw 20-byte test address -> 0x-prefixed hex (contract returns checksummed)."""
    return "0x" + bytes(addr).hex()


def _register(contract, direct_vm, requester, reference_url=REFERENCE_URL,
              reference_text=REFERENCE_TEXT, translation=TRANSLATION_TEXT):
    direct_vm.sender = requester
    return contract.register_job(
        SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, translation, reference_url, reference_text
    )


def _mock_assessment(direct_vm, verdict, accuracy, fluency, completeness,
                     reference_page="Reference page: Le renard brun rapide saute par-dessus le chien paresseux."):
    # NOTE (genlayer-test 0.30): mock_llm auto-parses a JSON-string mock into
    # a dict, but the v0.4 SDK's exec_prompt(response_format="json") expects
    # the LLM to return JSON *text*, so the payload must be double-encoded:
    # the mock layer parses once (back to JSON text) and the SDK parses again.
    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": reference_page},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": verdict, "accuracy": accuracy,
                    "fluency": fluency, "completeness": completeness,
                    "rationale": "Assessment matches the reference."})),
    )


def test_register_job(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/translationcheck.py")

    job_id = _register(contract, direct_vm, direct_alice)
    assert job_id == "job_0"
    assert contract.get_job_count() == 1

    j = contract.get_job(job_id)
    assert j["submitter"].lower() == _hex(direct_alice).lower()
    assert j["source_text"] == SOURCE_TEXT
    assert j["source_lang"] == SOURCE_LANG
    assert j["target_lang"] == TARGET_LANG
    assert j["translation_text"] == TRANSLATION_TEXT
    assert j["reference_url"] == REFERENCE_URL
    assert j["reference_text"] == REFERENCE_TEXT
    assert j["status"] == "OPEN"
    assert j["verdict"] == ""
    assert j["accuracy"] == 0
    assert j["fluency"] == 0
    assert j["completeness"] == 0
    assert j["total_score"] == 0
    assert j["assessed_ts"] == 0

    listed = contract.list_jobs()
    assert len(listed["jobs"]) == 1
    assert listed["jobs"][0]["id"] == "job_0"

    # A second submitter (bob) can register too - anyone may register
    job_id2 = _register(contract, direct_vm, direct_bob)
    assert job_id2 == "job_1"
    assert contract.get_job_count() == 2
    assert contract.get_job("job_1")["submitter"].lower() == _hex(direct_bob).lower()

    # Reference text alone (no URL) is accepted
    direct_vm.sender = direct_alice
    job_id3 = contract.register_job(
        "Bonjour le monde.", "French", "English", "Hello world.", "",
        "Reference: Hello world."
    )
    assert contract.get_job(job_id3)["reference_url"] == ""
    assert contract.get_job(job_id3)["status"] == "OPEN"

    # No reference at all is also accepted
    direct_vm.sender = direct_alice
    job_id4 = contract.register_job(
        "Guten Tag.", "German", "English", "Good day.", "", ""
    )
    assert contract.get_job(job_id4)["status"] == "OPEN"


def test_register_job_validation(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/translationcheck.py")
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("[EXPECTED] Source text is empty"):
        contract.register_job("   ", SOURCE_LANG, TARGET_LANG, TRANSLATION_TEXT, REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Source language is empty"):
        contract.register_job(SOURCE_TEXT, "", TARGET_LANG, TRANSLATION_TEXT, REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Target language is empty"):
        contract.register_job(SOURCE_TEXT, SOURCE_LANG, "  ", TRANSLATION_TEXT, REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Translation text is empty"):
        contract.register_job(SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, "", REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Reference URL must be http(s)"):
        contract.register_job(SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, TRANSLATION_TEXT, "ftp://example.com/x", REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Source text too long"):
        contract.register_job("x" * 5001, SOURCE_LANG, TARGET_LANG, TRANSLATION_TEXT, REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Translation text too long"):
        contract.register_job(SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, "x" * 5001, REFERENCE_URL, REFERENCE_TEXT)
    with direct_vm.expect_revert("[EXPECTED] Job not found: job_99"):
        contract.get_job("job_99")


def test_assess_accurate(direct_vm, direct_deploy, direct_alice):
    contract = direct_deploy("contracts/translationcheck.py")
    job_id = _register(contract, direct_vm, direct_alice)

    # Cannot assess a job that does not exist
    with direct_vm.expect_revert("[EXPECTED] Job not found: job_99"):
        contract.assess("job_99")

    _mock_assessment(direct_vm, "accurate", 95, 92, 90)
    result = contract.assess(job_id)
    assert result["verdict"] == "accurate"
    assert result["accuracy"] == 95
    assert result["fluency"] == 92
    assert result["completeness"] == 90
    assert result["total_score"] == 277
    j = contract.get_job(job_id)
    assert j["status"] == "ASSESSED"
    assert j["verdict"] == "accurate"
    assert j["total_score"] == 277
    assert j["assessed_ts"] > 0

    # Assessing twice reverts
    with direct_vm.expect_revert("[EXPECTED] Job is not open"):
        contract.assess(job_id)


def test_assess_all_verdicts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/translationcheck.py")
    j0 = _register(contract, direct_vm, direct_alice)
    j1 = _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    j2 = contract.register_job(
        SOURCE_TEXT, SOURCE_LANG, TARGET_LANG, TRANSLATION_TEXT, "", "Reference text only."
    )

    _mock_assessment(direct_vm, "needs_revision", 75, 60, 70)
    result = contract.assess(j0)
    assert result["verdict"] == "needs_revision"
    assert result["total_score"] == 205
    assert contract.get_job(j0)["status"] == "ASSESSED"

    direct_vm.clear_mocks()
    _mock_assessment(direct_vm, "inaccurate", 30, 25, 40)
    result = contract.assess(j1)
    assert result["verdict"] == "inaccurate"
    assert result["total_score"] == 95
    assert contract.get_job(j1)["status"] == "ASSESSED"

    # No-URL job assesses from reference text alone (no web mock needed)
    direct_vm.clear_mocks()
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "accurate", "accuracy": 88,
                    "fluency": 85, "completeness": 90,
                    "rationale": "Text reference is conclusive."})),
    )
    result = contract.assess(j2)
    assert result["verdict"] == "accurate"
    assert result["total_score"] == 263
    assert contract.get_job(j2)["status"] == "ASSESSED"


def test_assess_llm_validation(direct_vm, direct_deploy, direct_alice):
    """Invalid verdicts, out-of-range scores and verdict/score mismatches are LLM errors."""
    contract = direct_deploy("contracts/translationcheck.py")
    job_id = _register(contract, direct_vm, direct_alice)

    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": "Reference page."},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "maybe", "accuracy": 50, "fluency": 50,
                    "completeness": 50, "rationale": "Not valid."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] Verdict not one of"):
        contract.assess(job_id)

    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": "Reference page."},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "accurate", "accuracy": 60, "fluency": 90,
                    "completeness": 90, "rationale": "Accurate but low score."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] Accurate verdict with a score below"):
        contract.assess(job_id)

    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": "Reference page."},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "inaccurate", "accuracy": 70, "fluency": 70,
                    "completeness": 70, "rationale": "Inaccurate but high total."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] Inaccurate verdict with total_score above"):
        contract.assess(job_id)

    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": "Reference page."},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "needs_revision", "accuracy": 150, "fluency": 90,
                    "completeness": 90, "rationale": "Out of range."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] accuracy out of range 0-100"):
        contract.assess(job_id)

    direct_vm.clear_mocks()
    direct_vm.mock_web(
        r".*example\.com/translations/reference-001.*",
        {"status": 200, "body": "Reference page."},
    )
    direct_vm.mock_llm(
        r".*assessing a translation.*",
        json.dumps(json.dumps({"verdict": "needs_revision", "accuracy": 90, "fluency": 90,
                    "completeness": 90, "rationale": "All high but needs revision."})),
    )
    with direct_vm.expect_revert("[LLM_ERROR] Needs_revision verdict with accurate or inaccurate scores"):
        contract.assess(job_id)


def test_close_job(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    contract = direct_deploy("contracts/translationcheck.py")
    job_id = _register(contract, direct_vm, direct_alice)

    # The submitter (alice) is NOT the contract owner here (deployer is the
    # default test account), so closing as alice must revert
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the owner can close a job"):
        contract.close_job(job_id)

    # Owner closes an OPEN job
    direct_vm.sender = direct_owner
    contract.close_job(job_id)
    assert contract.get_job(job_id)["status"] == "CLOSED"

    # Cannot close twice
    with direct_vm.expect_revert("[EXPECTED] Job is already closed"):
        contract.close_job(job_id)

    # A closed job cannot be assessed
    with direct_vm.expect_revert("[EXPECTED] Job is not open"):
        contract.assess(job_id)


def test_close_after_assessment_and_withdraw(direct_vm, direct_deploy, direct_alice, direct_bob, direct_owner):
    contract = direct_deploy("contracts/translationcheck.py")
    job_id = _register(contract, direct_vm, direct_alice)
    _mock_assessment(direct_vm, "needs_revision", 75, 60, 70)
    result = contract.assess(job_id)
    assert result["verdict"] == "needs_revision"

    # Owner closes after assessment; the verdict and scores are kept
    direct_vm.sender = direct_owner
    contract.close_job(job_id)
    j = contract.get_job(job_id)
    assert j["status"] == "CLOSED"
    assert j["verdict"] == "needs_revision"
    assert j["total_score"] == 205

    # Withdraw: only the submitter, only while OPEN
    job2 = _register(contract, direct_vm, direct_bob)
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the submitter can withdraw a job"):
        contract.withdraw_job(job2)
    direct_vm.sender = direct_bob
    contract.withdraw_job(job2)
    assert contract.get_job(job2)["status"] == "CLOSED"

    # A withdrawn (closed) job cannot be withdrawn again
    with direct_vm.expect_revert("[EXPECTED] Only an open job can be withdrawn"):
        contract.withdraw_job(job2)
