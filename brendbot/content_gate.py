"""Content-gate primitives: classifier response parsing, outcome routing,
admin-bypass detection. Kept in a separate module from session.py so the
logic is testable without Session state.

Three public entry points:
  - detect_admin_bypass(text, tier, pattern_mode="edge") -> bool
  - parse_classifier_response(raw) -> ClassifierResult
  - decide_outcome(classifier_result, thresholds) -> Outcome

See engagement.yaml `content_gate:` block for config. See FUSED-CORE.md
GATE CHECK section for policy. Full criteria definitions in
docs/content-gate.md.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Outcome(str, Enum):
    """Three-outcome content gate result, plus BYPASS for admin italic-token
    path and FLOOR_HIT for hard-floor short-circuits."""
    PASS = "pass"
    FLAG = "flag"
    REFUSE = "refuse"
    BYPASS = "bypass"
    FLOOR_HIT = "floor_hit"


@dataclass
class ClassifierResult:
    """Parsed output of the content-gate classifier. Either criteria
    (scored band) or hard_floor (list match) may be populated; both
    being empty means benign."""
    criteria: dict[str, float] = field(default_factory=dict)
    hard_floor: str | None = None
    reasoning: str = ""
    parse_error: bool = False  # True if raw response was unparseable

    @property
    def weighted_sum(self) -> float:
        return sum(self.criteria.values())

    @property
    def is_benign(self) -> bool:
        return not self.criteria and self.hard_floor is None

    def to_dict(self) -> dict:
        """JSON-serializable form for audit logging."""
        return {
            "criteria": dict(self.criteria),
            "hard_floor": self.hard_floor,
            "reasoning": self.reasoning[:200],
            "weighted_sum": round(self.weighted_sum, 3),
            "parse_error": self.parse_error,
        }


# Admin-bypass token detection.
# Matches *brend* as a whole-word standalone token: at message start
# (optionally preceded by whitespace), at message end (optionally followed
# by trailing punctuation), or surrounded by whitespace. Mid-sentence
# emphasis like "not quite, *brend*, try again" is NOT matched because
# the surrounding comma is not in the trailing-punctuation set here —
# the comma attaches the token to the clause, making it mid-sentence.
# Only `. ! ? ;` and whitespace/end-of-string count as terminators.
_BYPASS_EDGE_RE = re.compile(
    r'(?:^|\s)\*brend\*(?:\s|$|[.!?;])',
    re.IGNORECASE,
)

# Alternative looser pattern (not used by default) — any *brend* anywhere
# in the message triggers. Preserved in case the edge pattern proves too
# strict in production.
_BYPASS_ANY_RE = re.compile(r'\*brend\*', re.IGNORECASE)


def detect_admin_bypass(
    text: str,
    tier: str,
    pattern_mode: str = "edge",
) -> bool:
    """Return True iff the message should invoke the admin content-gate
    bypass. Requires tier=='admin' AND a *brend* italic token in the
    text matching the selected pattern mode.

    pattern_mode:
      - 'edge' (default): token at message start, end, or as standalone
        whole-word. Mid-sentence emphasis does NOT trigger.
      - 'any': any *brend* anywhere in the message triggers (looser).

    Non-admin senders never trigger regardless of token presence. This
    is the only place the tier check happens — callers can trust the
    return value to be authoritative."""
    if tier != "admin":
        return False
    if not text:
        return False
    if pattern_mode == "any":
        return bool(_BYPASS_ANY_RE.search(text))
    return bool(_BYPASS_EDGE_RE.search(text))


# Classifier response parser.
#
# Expected format from the classifier (see content_classifier_prompt in
# engagement.yaml):
#
#   TRIGGERED: criterion_a=0.5, criterion_b=0.9
#   REASONING: one-sentence explanation
#
# OR for benign:
#
#   TRIGGERED: none
#   REASONING: benign request
#
# OR for hard floor:
#
#   TRIGGERED: hard_floor=<floor_name>
#   REASONING: explanation
#
# 2026-04-16 hardening: flag_audit analysis showed a ~40% parse-error rate
# on production classifier responses (8/20). Observed drift patterns:
#   - preamble prose before TRIGGERED ("Here's my analysis:\n...")
#   - markdown code fences (```\nTRIGGERED: ...\n```)
#   - bolded headers (**TRIGGERED:** none)
#   - trailing "Let me know if..." postamble
# We now (a) strip common markdown noise before regex parsing, (b) match
# TRIGGERED/REASONING anywhere in the blob (not strictly at line start),
# (c) accept bold/italic wrappers around the keyword. The prompt is also
# tightened to discourage drift — see engagement.yaml STRICT FORMAT block.

# Strip triple-backtick fences (optionally language-tagged).
_CODE_FENCE_RE = re.compile(r'```[a-zA-Z0-9_+-]*\n?|```')

_TRIGGERED_LINE_RE = re.compile(
    r'(?:^|\n)\s*TRIGGERED\s*:\s*(.+?)(?:\n|$)',
    re.IGNORECASE,
)
_REASONING_LINE_RE = re.compile(
    r'(?:^|\n)\s*REASONING\s*:\s*(.+?)(?:\n\s*\n|\n(?=\S+:)|$)',
    re.IGNORECASE | re.DOTALL,
)
_CRITERION_RE = re.compile(r'([a-z_]+)\s*=\s*([0-9.]+)')


def _strip_markdown_noise(raw: str) -> str:
    """Pre-clean common classifier drift patterns before regex parsing.

    Drops all asterisks and code fences. Asterisks have no semantic role
    in the structured response format — they only ever appear as drift
    decoration (`**TRIGGERED:**`, `*TRIGGERED:*`, etc.). Dropping them
    globally is safer than trying to match every bold/italic pattern.

    Code fences are stripped as whole tokens. Any remaining content
    (including the TRIGGERED/REASONING lines inside a fenced block)
    passes through unchanged.
    """
    cleaned = _CODE_FENCE_RE.sub('', raw)
    cleaned = cleaned.replace('*', '')
    return cleaned


def parse_classifier_response(raw: str) -> ClassifierResult:
    """Parse the classifier's structured text output.

    Fail-loud: if the response is unparseable, return a ClassifierResult
    with parse_error=True and criteria={'_parse_error': 2.0}. The caller
    should route this as if it tripped refuse_threshold — fail-conservative.

    If the classifier returns nothing or only whitespace, returns a
    benign result with parse_error=True (unusable, treat as refuse-band).

    Tolerates common drift patterns: markdown code fences, bolded keywords,
    leading preamble. These account for the bulk of the 40% parse-error
    rate seen in flag_audit 2026-04 — the classifier is mostly right on
    content but wrong on formatting discipline.
    """
    if not raw or not raw.strip():
        return ClassifierResult(
            criteria={"_parse_error": 2.0},
            reasoning="empty classifier response",
            parse_error=True,
        )

    cleaned = _strip_markdown_noise(raw)

    triggered_match = _TRIGGERED_LINE_RE.search(cleaned)
    reasoning_match = _REASONING_LINE_RE.search(cleaned)
    reasoning = reasoning_match.group(1).strip() if reasoning_match else ""

    if not triggered_match:
        # No TRIGGERED line at all — classifier malfunctioned.
        return ClassifierResult(
            criteria={"_parse_error": 2.0},
            reasoning=f"no TRIGGERED line in response: {raw[:100]}",
            parse_error=True,
        )

    triggered_content = triggered_match.group(1).strip()
    # Strip stray trailing markdown artifacts (e.g. "**", trailing commas).
    triggered_content = triggered_content.rstrip('*, \t')
    result = ClassifierResult(reasoning=reasoning)

    # Benign case.
    if triggered_content.lower() == "none":
        return result

    # Hard-floor case. Form: "hard_floor=<floor_name>"
    if triggered_content.lower().startswith("hard_floor="):
        floor_name = triggered_content.split("=", 1)[1].strip().strip(",;").lower()
        if floor_name:
            result.hard_floor = floor_name
        return result

    # Scored criteria. Form: "name=weight, name=weight, ..."
    for m in _CRITERION_RE.finditer(triggered_content):
        name = m.group(1).lower()
        try:
            weight = float(m.group(2))
        except ValueError:
            continue
        # Skip obvious noise matches — the regex can hit REASONING tokens
        # or keywords if the classifier deviated from the format.
        if name in ("triggered", "reasoning", "none", "hard_floor"):
            continue
        result.criteria[name] = weight

    if not result.criteria:
        # TRIGGERED present but no parseable criteria — another parse error.
        result.parse_error = True
        result.criteria = {"_parse_error": 2.0}
        result.reasoning = f"unparseable TRIGGERED content: {triggered_content[:100]}"

    return result


def decide_outcome(
    classifier_result: ClassifierResult,
    hard_floors: set[str],
    pass_threshold: float,
    flag_threshold: float,
    refuse_threshold: float,
) -> Outcome:
    """Route a classifier result to a PASS / FLAG / REFUSE / FLOOR_HIT
    outcome based on weighted sum and hard-floor match.

    Precedence:
      1. hard_floor set AND in the configured hard_floors list → FLOOR_HIT
      2. weighted_sum > refuse_threshold → REFUSE
      3. weighted_sum > pass_threshold AND ≤ flag_threshold → FLAG
      4. weighted_sum ≤ pass_threshold → PASS

    Boundary semantics:
      - PASS is inclusive of pass_threshold (sum ≤ pass_threshold → PASS)
      - FLAG is (pass_threshold, flag_threshold]
      - REFUSE is (refuse_threshold, ∞)

    A parse-error ClassifierResult carries a synthetic criterion
    '_parse_error'=2.0 that lands it above refuse_threshold (1.5 by
    default), so unparseable responses fail conservative to REFUSE.
    """
    if classifier_result.hard_floor and classifier_result.hard_floor in hard_floors:
        return Outcome.FLOOR_HIT

    weighted = classifier_result.weighted_sum
    if weighted > refuse_threshold:
        return Outcome.REFUSE
    if weighted > pass_threshold:
        return Outcome.FLAG
    return Outcome.PASS


def format_refusal_explanation(classifier_result: ClassifierResult) -> str:
    """Generate a user-facing refusal message from a REFUSE or FLOOR_HIT
    classifier result. Keeps the mechanism language short — names the
    tripped criteria in plain terms, no mechanism jargon.

    REFUSE: lists tripped criteria by category.
    FLOOR_HIT: names the hard floor in plain language.
    """
    if classifier_result.hard_floor:
        floor = classifier_result.hard_floor
        floor_plain = {
            "minor_sexual": "sexual content involving a minor",
            "wmd_synth": "weapons of mass destruction synthesis",
            "malware": "malware or exploit code",
            "infra_attack": "infrastructure attack procedures",
            "extremist_recruit": "extremist recruitment material",
            "directed_incite": "directed incitement against a real person",
        }.get(floor, floor.replace("_", " "))
        return f"can't do that one — hard floor: {floor_plain}."

    if classifier_result.parse_error:
        return "can't do that one — safety classifier returned unparseable output, failing conservative."

    # Generate a compact plain-language summary of the tripped criteria.
    names = list(classifier_result.criteria.keys())
    if not names:
        return "can't do that one — safety gate."

    plain_parts: list[str] = []
    if any(n.startswith("tragedy_") for n in names):
        plain_parts.append("real tragedy")
    if "person_targeted" in names:
        plain_parts.append("targeted real person")
    elif any(n.startswith("person_") for n in names):
        plain_parts.append("named real person")
    if "frame_directed" in names:
        plain_parts.append("directed framing")
    elif "frame_ambiguous" in names:
        plain_parts.append("ambiguous framing")

    if not plain_parts:
        plain_parts = [n.replace("_", " ") for n in names[:3]]

    return f"can't do that one — {' + '.join(plain_parts)} stacks. safety gate holds."
