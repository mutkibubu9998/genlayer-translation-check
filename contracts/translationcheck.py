# { "Depends": "py-genlayer:kzybg95x1gn21dw72m7hyg0maaap04n4nx55j9yt6fkdqxtz6bz0" }

"""TranslationCheck - translation quality assessment by GenLayer consensus.

Anyone registers a translation job: a source text, its source language,
the target language, the translation text to assess, and an optional
reference (a URL and/or free text, e.g. a reference translation or
context). Anyone can then trigger assessment. The leader fetches the
reference page (when a URL is given) and an LLM assesses the
translation, returning strict JSON: a verdict (accurate /
needs_revision / inaccurate), scores 0-100 for accuracy, fluency and
completeness, plus a short rationale. The total score is the
deterministic sum of the three scores.

Every validator independently re-fetches the same reference and
re-runs the same assessment; the assessment is accepted ONLY when the
verdict matches exactly AND the total score agrees within +/-10
points, so no single leader can set a verdict alone. Free-text
rationales may differ.

State machine:
    OPEN -> ASSESSED       (consensus assessment stored)
    OPEN -> CLOSED         (contract owner closes, or submitter withdraws)
    ASSESSED -> CLOSED     (contract owner closes after assessment)

No money moves in this contract at all: no GEN transfers, no payments,
no payable methods. It is a pure assessment primitive - the same
contract serves any translation because the languages, the texts, and
the reference are per-job inputs, not hard-coded prompts.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import genlayer as gl
from genlayer import *
from genlayer.storage import DynArray, TreeMap, allow

# ---------------------------------------------------------------------------
# Error classification (validators compare these prefixes, see _handle_leader_error)
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"  # Business logic (deterministic) - exact match required
ERROR_EXTERNAL = "[EXTERNAL]"  # External API 4xx (deterministic) - exact match required
ERROR_TRANSIENT = "[TRANSIENT]"  # Network/5xx (non-deterministic) - agree if both transient
ERROR_LLM = "[LLM_ERROR]"  # LLM misbehavior - always disagree, force rotation

MAX_SOURCE_TEXT_LEN = 5000
MAX_TRANSLATION_LEN = 5000
MAX_LANG_LEN = 50
MAX_REFERENCE_URL_LEN = 500
MAX_REFERENCE_TEXT_LEN = 5000
REFERENCE_CHARS = 12000
VERDICTS = ("accurate", "needs_revision", "inaccurate")
TOTAL_TOLERANCE = 10
ACCURATE_MIN_SCORE = 80  # accurate requires every score >= 80
INACCURATE_MAX_TOTAL = 120  # inaccurate requires total <= 120


@allow
@dataclass
class Job:
    id: str
    submitter: Address
    source_text: str
    source_lang: str
    target_lang: str
    translation_text: str
    reference_url: str
    reference_text: str
    status: str  # OPEN | ASSESSED | CLOSED
    verdict: str  # "" | accurate | needs_revision | inaccurate
    accuracy: u256  # 0-100
    fluency: u256  # 0-100
    completeness: u256  # 0-100
    total_score: u256  # 0-300, deterministic sum of the three scores
    rationale: str
    created_ts: u256
    assessed_ts: u256


class TranslationCheck(gl.contract.Contract):
    owner: Address
    job_count: u256
    jobs: TreeMap[str, Job]
    job_ids: DynArray[str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.job_count = u256(0)

    # ------------------------------------------------------------------
    # Helpers (deterministic)
    # ------------------------------------------------------------------
    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_job(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Job not found: {job_id}"
            )
        return self.jobs[job_id]

    def _job_to_dict(self, j: Job) -> dict:
        return {
            "id": j.id,
            "submitter": j.submitter.as_hex,
            "source_text": j.source_text,
            "source_lang": j.source_lang,
            "target_lang": j.target_lang,
            "translation_text": j.translation_text,
            "reference_url": j.reference_url,
            "reference_text": j.reference_text,
            "status": j.status,
            "verdict": j.verdict,
            "accuracy": int(j.accuracy),
            "fluency": int(j.fluency),
            "completeness": int(j.completeness),
            "total_score": int(j.total_score),
            "rationale": j.rationale,
            "created_ts": int(j.created_ts),
            "assessed_ts": int(j.assessed_ts),
        }

    def _fetch_reference(self, url: str) -> str:
        """Fetch one URL, classifying failures for consensus."""
        res = gl.nondet.web.get(url)
        if res.status == 200:
            pass
        elif 400 <= res.status < 500:
            raise gl.vm.UserError(f"{ERROR_EXTERNAL} URL returned {res.status}: {url}")
        else:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} URL returned {res.status}: {url}")
        try:
            return res.body.decode("utf-8", errors="ignore")[:REFERENCE_CHARS]
        except Exception:
            raise gl.vm.UserError(f"{ERROR_TRANSIENT} Reference undecodable: {url}")

    def _parse_score(self, data: dict, key: str) -> int:
        """Extract one 0-100 score from LLM JSON, with key aliasing."""
        raw = data.get(key)
        if raw is None:
            for alt in (key + "_score", key + "_rating", "score_" + key):
                if alt in data:
                    raw = data[alt]
                    break
        if raw is None:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Missing '{key}'. Keys: {list(data.keys())}"
            )
        try:
            value = int(round(float(str(raw).strip())))
        except (ValueError, TypeError):
            raise gl.vm.UserError(f"{ERROR_LLM} Non-numeric {key}: {raw}")
        if value < 0 or value > 100:
            raise gl.vm.UserError(
                f"{ERROR_LLM} {key} out of range 0-100: {value}"
            )
        return value

    def _parse_assessment(self, raw) -> dict:
        """Defensively parse the LLM assessment JSON (dict or raw string)."""
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw
            first = text.find("{")
            last = text.rfind("}")
            if first < 0 or last < 0:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
            text = text[first : last + 1]
            text = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)
            try:
                data = json.loads(text)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON unparsable")
        else:
            raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

        if not isinstance(data, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON is not an object")

        verdict = data.get("verdict")
        if verdict is None:
            for alt in ("decision", "outcome", "result"):
                if alt in data:
                    verdict = data[alt]
                    break
        if verdict is None:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Missing 'verdict'. Keys: {list(data.keys())}"
            )
        if not isinstance(verdict, str):
            verdict = str(verdict)
        verdict = verdict.strip().lower()
        if verdict not in VERDICTS:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Verdict not one of {VERDICTS}: {verdict}"
            )

        accuracy = self._parse_score(data, "accuracy")
        fluency = self._parse_score(data, "fluency")
        completeness = self._parse_score(data, "completeness")
        total_score = accuracy + fluency + completeness

        # Verdict and scores must be consistent.
        if verdict == "accurate" and (
            accuracy < ACCURATE_MIN_SCORE
            or fluency < ACCURATE_MIN_SCORE
            or completeness < ACCURATE_MIN_SCORE
        ):
            raise gl.vm.UserError(
                f"{ERROR_LLM} Accurate verdict with a score below {ACCURATE_MIN_SCORE}: "
                f"accuracy={accuracy} fluency={fluency} completeness={completeness}"
            )
        if verdict == "inaccurate" and total_score > INACCURATE_MAX_TOTAL:
            raise gl.vm.UserError(
                f"{ERROR_LLM} Inaccurate verdict with total_score above {INACCURATE_MAX_TOTAL}: {total_score}"
            )
        if verdict == "needs_revision" and (
            (
                accuracy >= ACCURATE_MIN_SCORE
                and fluency >= ACCURATE_MIN_SCORE
                and completeness >= ACCURATE_MIN_SCORE
            )
            or total_score <= INACCURATE_MAX_TOTAL
        ):
            raise gl.vm.UserError(
                f"{ERROR_LLM} Needs_revision verdict with accurate or inaccurate scores: "
                f"accuracy={accuracy} fluency={fluency} completeness={completeness}"
            )

        rationale = data.get("rationale")
        if rationale is None:
            for alt in ("reason", "explanation", "summary"):
                if alt in data:
                    rationale = data[alt]
                    break
        if rationale is None:
            rationale = ""
        if not isinstance(rationale, str):
            rationale = str(rationale)
        return {
            "verdict": verdict,
            "accuracy": accuracy,
            "fluency": fluency,
            "completeness": completeness,
            "total_score": total_score,
            "rationale": rationale[:500],
        }

    def _error_text(self, res) -> str:
        """Extract the message text from a VM Result (v0.4 SDK: UserError carries .data, VMError carries .message)."""
        if isinstance(res, gl.vm.UserError):
            data = res.data
            return data if isinstance(data, str) else str(data)
        msg = getattr(res, "message", None)
        return msg if isinstance(msg, str) else str(res)

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical error comparison for validators (see write-contract skill)."""
        leader_msg = self._error_text(leaders_res)
        try:
            leader_fn()
            return False  # Leader errored, validator succeeded - disagree
        except gl.vm.UserError as e:
            validator_msg = self._error_text(e)
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
                ERROR_TRANSIENT
            ):
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Register a translation job (anyone may register)
    # ------------------------------------------------------------------
    @gl.public.write
    def register_job(
        self,
        source_text: str,
        source_lang: str,
        target_lang: str,
        translation_text: str,
        reference_url: str,
        reference_text: str,
    ) -> str:
        source_text = source_text.strip()
        if len(source_text) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Source text is empty")
        if len(source_text) > MAX_SOURCE_TEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Source text too long")

        source_lang = source_lang.strip()
        if len(source_lang) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Source language is empty")
        if len(source_lang) > MAX_LANG_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Source language too long")

        target_lang = target_lang.strip()
        if len(target_lang) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Target language is empty")
        if len(target_lang) > MAX_LANG_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Target language too long")

        translation_text = translation_text.strip()
        if len(translation_text) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Translation text is empty")
        if len(translation_text) > MAX_TRANSLATION_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Translation text too long")

        reference_url = reference_url.strip()
        if len(reference_url) > MAX_REFERENCE_URL_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Reference URL too long")
        if len(reference_url) > 0 and not (
            reference_url.startswith("http://") or reference_url.startswith("https://")
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Reference URL must be http(s)")

        reference_text = reference_text.strip()
        if len(reference_text) > MAX_REFERENCE_TEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Reference text too long")

        job_id = f"job_{int(self.job_count)}"
        job = Job(
            id=job_id,
            submitter=gl.message.sender_address,
            source_text=source_text,
            source_lang=source_lang,
            target_lang=target_lang,
            translation_text=translation_text,
            reference_url=reference_url,
            reference_text=reference_text,
            status="OPEN",
            verdict="",
            accuracy=u256(0),
            fluency=u256(0),
            completeness=u256(0),
            total_score=u256(0),
            rationale="",
            created_ts=self._now(),
            assessed_ts=u256(0),
        )
        self.jobs[job_id] = job
        self.job_ids.append(job_id)
        self.job_count = self.job_count + u256(1)
        return job_id

    # ------------------------------------------------------------------
    # Assess a translation by consensus
    # ------------------------------------------------------------------
    @gl.public.write
    def assess(self, job_id: str) -> dict:
        job = self._get_job(job_id)
        if job.status != "OPEN":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Job is not open: {job.status}"
            )

        source_text = job.source_text
        source_lang = job.source_lang
        target_lang = job.target_lang
        translation_text = job.translation_text
        reference_url = job.reference_url
        reference_text = job.reference_text

        def leader_fn() -> dict:
            reference_page = ""
            if len(reference_url) > 0:
                reference_page = self._fetch_reference(reference_url)

            prompt = f"""You are assessing a translation. Judge ONLY from the source text and the reference below; do not assume facts that are not in them.
Source language: {source_lang}
Target language: {target_lang}
Source text: {source_text}
Translation to assess: {translation_text}
Reference translation or context text: {reference_text if len(reference_text) > 0 else "(none)"}
Reference page fetched from {reference_url if len(reference_url) > 0 else "(no URL given)"}:
{reference_page if len(reference_page) > 0 else "(no page fetched)"}

Score the translation 0-100 on accuracy (meaning preserved), fluency (natural target-language wording) and completeness (nothing important omitted or added). Decide the verdict and respond with JSON only, nothing else:
{{"verdict": "<one of: accurate, needs_revision, inaccurate>", "accuracy": <integer 0-100>, "fluency": <integer 0-100>, "completeness": <integer 0-100>, "rationale": "<one sentence summarising the assessment>"}}

Verdict meanings and score rules:
- accurate: a faithful, natural, complete translation; every score must be 80 or above
- needs_revision: usable but flawed - wrong wording, awkward phrasing or missing detail; total of the three scores must be above 120 and at least one score below 80
- inaccurate: meaning is lost or badly distorted; total of the three scores must be 120 or below"""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return self._parse_assessment(raw)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            leader_data = leaders_res.calldata
            if not isinstance(leader_data, dict):
                return False
            # Agreement rule: verdict must match exactly, and the total
            # score must agree within +/-10 points. Rationale wording and
            # individual scores may differ.
            if leader_data.get("verdict") != mine.get("verdict"):
                return False
            leader_total = leader_data.get("total_score")
            mine_total = mine.get("total_score")
            if leader_total is None or mine_total is None:
                return False
            if abs(int(leader_total) - int(mine_total)) > TOTAL_TOLERANCE:
                return False
            return True

        result = gl.vm.run_nondet(leader_fn, validator_fn)

        job.verdict = result["verdict"]
        job.accuracy = u256(result["accuracy"])
        job.fluency = u256(result["fluency"])
        job.completeness = u256(result["completeness"])
        job.total_score = u256(result["total_score"])
        job.rationale = result.get("rationale", "")
        job.status = "ASSESSED"
        job.assessed_ts = self._now()
        self.jobs[job_id] = job
        return {
            "verdict": job.verdict,
            "accuracy": int(job.accuracy),
            "fluency": int(job.fluency),
            "completeness": int(job.completeness),
            "total_score": int(job.total_score),
            "rationale": job.rationale,
        }

    # ------------------------------------------------------------------
    # Contract owner closes a job (OPEN or ASSESSED)
    # ------------------------------------------------------------------
    @gl.public.write
    def close_job(self, job_id: str) -> None:
        job = self._get_job(job_id)
        if gl.message.sender_address != self.owner:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the owner can close a job"
            )
        if job.status == "CLOSED":
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Job is already closed")

        job.status = "CLOSED"
        self.jobs[job_id] = job

    # ------------------------------------------------------------------
    # Submitter withdraws their own OPEN job
    # ------------------------------------------------------------------
    @gl.public.write
    def withdraw_job(self, job_id: str) -> None:
        job = self._get_job(job_id)
        if gl.message.sender_address != job.submitter:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only the submitter can withdraw a job"
            )
        if job.status != "OPEN":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Only an open job can be withdrawn: {job.status}"
            )

        job.status = "CLOSED"
        self.jobs[job_id] = job

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_job(self, job_id: str) -> dict:
        return self._job_to_dict(self._get_job(job_id))

    @gl.public.view
    def list_jobs(self) -> dict:
        return {
            "jobs": [
                self._job_to_dict(self.jobs[jid]) for jid in self.job_ids
            ]
        }

    @gl.public.view
    def get_job_count(self) -> u256:
        return self.job_count
