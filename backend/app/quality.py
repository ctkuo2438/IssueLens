"""Narrow English checks for model claims, draft voice and label applicability."""

from collections.abc import Iterable
import re

from backend.app.models import AnalysisContent, CommentCheck, RepositoryLabel


_PR_TERM = r"(?:pull[\s-]+requests?|prs?)"
# Match stated label scope, rather than every mention of a pull request.
PR_ONLY_SCOPE = re.compile(
    rf"^(?:(?:this\s+label\s+is\s+)?(?:used\s+|intended\s+|reserved\s+)?for\s+)?{_PR_TERM}\b"
    r"(?=\s*(?:$|[.:;]|only\b|that\b|which\b|for\b|with\b|updating\b|fixing\b|adding\b))"
    rf"|\b(?:only|exclusively)\s+(?:(?:for|on)\s+)?{_PR_TERM}\b"
    rf"|\b{_PR_TERM}[\s-]+only\b"
    rf"|\b(?:restricted|limited|reserved|applicable|applies)\s+(?:only\s+)?to\s+{_PR_TERM}\b",
    re.IGNORECASE,
)
SHARED_LABEL_SCOPE = re.compile(
    rf"\b{_PR_TERM}\s*(?:[/&]|\band\b|\bor\b|,\s*(?:(?:and|or)\s+)?)\s*(?:github\s+)?issues?\b"
    rf"|\bissues?\s*(?:[/&]|\band\b|\bor\b|,\s*(?:(?:and|or)\s+)?)\s*{_PR_TERM}\b",
    re.IGNORECASE,
)
LABEL_SCOPE_NEGATION = re.compile(
    r"\b(?:not|never|isn['’]t|aren['’]t)(?:\s+(?:just|a|an|the|for))*\s*$",
    re.IGNORECASE,
)


def is_pr_only_label(description: str) -> bool:
    """Recognize known explicit English PR-only scopes, not general label relevance."""
    text = " ".join(description.split())
    if SHARED_LABEL_SCOPE.search(text):
        return False
    return any(
        not LABEL_SCOPE_NEGATION.search(text[max(0, match.start() - 60):match.start()])
        for match in PR_ONLY_SCOPE.finditer(text)
    )


def pr_only_recommendations(content: AnalysisContent, labels: Iterable[RepositoryLabel]) -> list[str]:
    """Return canonical names whose retrieved descriptions conflict with an issue target."""
    blocked = {label.name.casefold(): label.name for label in labels if is_pr_only_label(label.description)}
    return [blocked[item.name.casefold()] for item in content.label_recommendations if item.name.casefold() in blocked]


# This catches known positive first-person forms, not all unsupported execution claims.
# Negations such as "I have not reproduced" are deliberately not matched.
REPRODUCTION_CLAIM = re.compile(
    r"\b(?:I|we)(?:['’](?:ve|m|re))?\s+"
    r"(?:(?:can|could|did|have|had|am|are|was|were|been|able|to|managed|"
    r"successfully|personally|locally|reliably|consistently|also)\s+)*"
    r"(?:reproduc(?:e|ed|ing)|replicat(?:e|ed|ing))\b",
    re.IGNORECASE,
)


# This targets known assistant service offers, not all first-person maintainer language.
DRAFT_SERVICE_OFFER = re.compile(
    r"\b(?:"
    r"if\s+you\s+(?:want|would\s+like)\s*[,;:]?\s*(?:i|we)\s+(?:can|could)\s+help\b"
    r"|if\s+you['’]d\s+like\s*[,;:]?\s*(?:i|we)\s+(?:can|could)\s+help\b"
    r"|(?:i|we)\s+(?:can|could)\s+help\s+(?:you\s+)?(?:turn|draft|write|create|prepare|generate)\b"
    r"|(?:would|do)\s+you\s+like\s+me\s+to\s+(?:help|draft|write|create|prepare|generate)\b"
    r"|let\s+me\s+know\s+if\s+you(?:['’]d|\s+would)?\s+(?:like|want)\s+me\s+to\s+"
    r"(?:help|draft|write|create|prepare|generate)\b"
    r")",
    re.IGNORECASE,
)


def draft_has_service_offer(text: str) -> bool:
    """Detect known meta-conversation in the draft, without rewriting model output."""
    return DRAFT_SERVICE_OFFER.search(text) is not None


# Current tools cannot perform any of these repository mutations. Match a GitHub
# object as well as a verb, so closing a connection is not a tracker action.
_DRAFT_GITHUB_OBJECT = (
    r"(?:(?:(?:this|that|the|an?|your|our)\s+)?"
    r"(?:issue|report|ticket|thread|pull[\s-]+request|pr|comment|reply)\b"
    r"|(?:this|that|it)\b|#\d+\b)"
)
DRAFT_ACTION_CLAIM = re.compile(
    r"\b(?:i|we)(?:['’](?:m|re|ve)|\s+(?:am|are|have|had))?\s+"
    r"(?:(?:just|already|now|also|successfully)\s+){0,2}(?:"
    r"(?:clos(?:e|ed|ing)|reopen(?:ed|ing)?|merg(?:e|ed|ing)|"
    r"label(?:ed|led|ing|ling)?|assign(?:ed|ing)?|post(?:ed|ing)?|publish(?:ed|ing)?)\s+"
    + _DRAFT_GITHUB_OBJECT
    + r"|(?:add(?:ed|ing)?|appl(?:y|ied|ying)|remov(?:e|ed|ing))\s+"
    r"(?:(?:the|a|this)\s+)?(?:(?:`[^`\n]{1,60}`|[\w:/.-]{1,60})\s+)?labels?\b)",
    re.IGNORECASE,
)
DRAFT_QUOTED_ACTION_SOURCE = re.compile(
    r"(?:\b(?:maintainers?|reporters?|authors?|contributors?|user\s+[\w-]+)|@[\w-]+)"
    r"\s+(?:said|wrote|reported|stated|says|writes|reports|states)"
    r"\s*[:,]?\s*(?:that\s+)?['\"“‘`]\s*$",
    re.IGNORECASE,
)


def draft_action_claims(text: str) -> list[str]:
    """Detect known first-person tracker writes, not suggested or reported actions."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if sentence.rstrip().endswith("?"):
            continue
        for claim in DRAFT_ACTION_CLAIM.finditer(sentence):
            if not DRAFT_QUOTED_ACTION_SOURCE.search(sentence[:claim.start()]):
                return ["unperformed_maintainer_action"]
    return []


_DRAFT_RESOLUTION = r"(?:fixed|resolved|implemented|handled|addressed)"
DRAFT_CLOSURE_PROOF = re.compile(
    r"\b(?P<state>closed)\b"
    r"(?:\s+(?:issue|report|ticket|state|status|alone|automatically|necessarily|by\s+itself)){0,3}"
    r"\s*[,;:]?\s*(?:(?:and|which)\s+)?"
    r"(?:(?:(?:does|did)(?:\s+not)?|doesn['’]t|didn['’]t|must)\s+)?"
    r"(?:(?:necessarily|automatically)\s+)?"
    r"\b(?P<link>means?|proves?|confirms?|shows?|implies|so|therefore|thus|hence)\b"
    rf"[^.!?\n]{{0,65}}?\b(?P<result>{_DRAFT_RESOLUTION})\b",
    re.IGNORECASE,
)
DRAFT_RESOLUTION_FROM_CLOSURE = re.compile(
    rf"\b(?P<result>{_DRAFT_RESOLUTION})\b[^.!?\n]{{0,45}}?"
    r"\b(?P<link>because|since|as)\b[^.!?\n]{0,45}?\b(?P<state>closed)\b",
    re.IGNORECASE,
)
DRAFT_CLOSED_RESOLUTION_PAIR = re.compile(
    r"\b(?P<state>closed)\s*(?:and|as|=|/)\s*(?:already\s+)?"
    rf"(?P<result>{_DRAFT_RESOLUTION})\b",
    re.IGNORECASE,
)
DRAFT_TRACKER_RESOLUTION = re.compile(
    r"\b(?P<result>resolved(?:\s*/\s*handled)?|handled)\s+in\s+(?:the\s+)?(?:issue\s+)?tracker\b",
    re.IGNORECASE,
)
DRAFT_CLAIM_NEGATION = re.compile(
    r"\b(?:not|never|cannot|can['’]t|doesn['’]t|didn['’]t)"
    r"(?:\s+(?:necessarily|automatically|already|by\s+itself))?\s*$",
    re.IGNORECASE,
)
DRAFT_INFERENCE_UNCERTAINTY = re.compile(
    r"\b(?:cannot|can['’]t|do\s+not|don['’]t|does\s+not|doesn['’]t)\s+"
    r"(?:assume|confirm|conclude|verify|establish|prove|know)\b",
    re.IGNORECASE,
)
DRAFT_UNASSERTED_RELATION_PREFIX = re.compile(
    r"\b(?:whether|if|(?:cannot|can['’]t|do\s+not|don['’]t)\s+"
    r"(?:assume|confirm|conclude|verify|establish|claim|say)(?:\s+(?:that|whether))?)\s+"
    r"(?:(?:a|the|this|that|it|issue|report|ticket|status|is|was|being)\s+){0,5}$",
    re.IGNORECASE,
)
DRAFT_REPORTED_CLAIM_PREFIX = re.compile(
    r"(?:\b(?:maintainers?|reporters?|authors?|contributors?|user\s+[\w-]+)|@[\w-]+)"
    r"\s+(?:said|wrote|reported|stated|says|writes|reports|states)"
    r"[^,;.!?\n]{0,45}$",
    re.IGNORECASE,
)


def draft_closure_claims(text: str) -> list[str]:
    """Flag known closure-to-fix inferences, not verify code or release status."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if sentence.rstrip().endswith("?"):
            continue
        for pattern in (DRAFT_CLOSURE_PROOF, DRAFT_RESOLUTION_FROM_CLOSURE,
                        DRAFT_CLOSED_RESOLUTION_PAIR, DRAFT_TRACKER_RESOLUTION):
            for claim in pattern.finditer(sentence):
                prefix = sentence[:claim.start()]
                if DRAFT_REPORTED_CLAIM_PREFIX.search(prefix) or DRAFT_UNASSERTED_RELATION_PREFIX.search(prefix):
                    continue
                # Negation must qualify this relation or outcome, not merely
                # occur somewhere else in the sentence or a prior sentence.
                if any(DRAFT_CLAIM_NEGATION.search(sentence[:claim.start(group)])
                       for group in claim.groupdict()):
                    continue
                if DRAFT_INFERENCE_UNCERTAINTY.search(claim.group()):
                    continue
                return ["closure_implies_resolution"]
    return []


DRAFT_FIX_FORECAST = re.compile(
    r"\b(?:will|is\s+expected\s+to|is\s+planned\s+to|is\s+scheduled\s+to)\s+be\s+"
    r"(?:fixed|resolved|addressed|corrected|released)\b"
    r"|\b(?:fix|patch|change)\s+(?:will|is\s+expected\s+to)\s+(?:land|ship|arrive)\b"
    r"|\bwe\s+(?:will|are\s+going\s+to)\s+(?:fix|resolve|address|release)\b"
    r"|\bshould\s+be\s+(?:fixed|resolved|addressed|corrected)\b(?=.{0,100}(?:#\d+|\bv?\d+\.\d+))",
    re.IGNORECASE,
)
DRAFT_CURRENT_GAP = re.compile(
    r"\b(?:does\s+not|doesn['’]t)\s+(?:currently\s+|yet\s+)?"
    r"(?:support|expose|implement)\b",
    re.IGNORECASE,
)
DRAFT_RELEASE_CLAIM = re.compile(
    r"\b(?:is|was|has\s+been)\s+(?:already\s+)?(?:fixed|resolved|implemented)\s+in\s+"
    r"(?:(?:version|release|[a-z][\w-]*)\s+)?v?\d+\.\d+\b",
    re.IGNORECASE,
)
DRAFT_SOURCE_ATTRIBUTION = re.compile(
    r"\b(?:maintainers?|reporters?|authors?|contributors?|comments?|discussion|readme|documentation)\b.{0,80}"
    r"\b(?:said|stated|wrote|reported|noted|expected|planned|suggested|says|states|reports|notes|describes)\b"
    r"|\baccording\s+to\s+(?:(?:the|a)\s+)?(?:maintainer|reporter|author|contributor|comment|readme|documentation)\b",
    re.IGNORECASE,
)
DRAFT_UNVERIFIED_CONTEXT = re.compile(
    r"\b(?:cannot|can['’]t|do\s+not|don['’]t|have\s+not|haven['’]t)\s+"
    r"(?:confirm|confirmed|verify|verified|promise|say)\b.{0,35}$",
    re.IGNORECASE,
)


def draft_temporal_claims(text: str) -> list[str]:
    """Flag known unqualified fix/support assertions, not general semantic grounding."""
    problems = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if sentence.rstrip().endswith("?"):
            continue
        for name, pattern in (
            ("unattributed_fix_forecast", DRAFT_FIX_FORECAST),
            ("unattributed_current_capability", DRAFT_CURRENT_GAP),
            ("unattributed_release_claim", DRAFT_RELEASE_CLAIM),
        ):
            for claim in pattern.finditer(sentence):
                prefix = sentence[:claim.start()]
                if DRAFT_SOURCE_ATTRIBUTION.search(prefix) or DRAFT_UNVERIFIED_CONTEXT.search(prefix):
                    continue
                if name == "unattributed_current_capability" and re.search(
                    r"\b(?:example|snippet|reproduction|test)\s*$", prefix, re.IGNORECASE,
                ):
                    continue
                problems.append(name)
                break
    return list(dict.fromkeys(problems))


def _analysis_texts(content: AnalysisContent) -> list[tuple[str, str]]:
    """Scan model prose only; source excerpts are never rewritten or graded here."""
    texts = [("summary", content.summary.text), ("draft_reply", content.draft_reply)]
    texts.extend((f"known_facts[{index}]", fact.text) for index, fact in enumerate(content.known_facts))
    texts.extend((f"missing_information[{index}]", text) for index, text in enumerate(content.missing_information))
    texts.extend((f"limitations[{index}]", text) for index, text in enumerate(content.limitations))
    for index, comparison in enumerate(content.comparisons):
        texts.append((f"comparisons[{index}].reason", comparison.reason))
        texts.extend((f"comparisons[{index}].similarities[{item}]", text) for item, text in enumerate(comparison.similarities))
        texts.extend((f"comparisons[{index}].differences[{item}]", text) for item, text in enumerate(comparison.differences))
    texts.extend((f"label_recommendations[{index}].reason", item.reason) for index, item in enumerate(content.label_recommendations))
    return texts


_LABEL_PHRASE = r"(?:(?:additional|new|further|other|suitable|supported)\s+){0,3}labels?"
# These patterns assert no current recommendations, not that no GitHub labels were applied.
# The "made" form requires "recommendation(s)"; "labels were made or added"
# describes a different claim and must not be treated as a zero-recommendation decision.
NO_LABEL_RECOMMENDATIONS = re.compile(
    rf"\bno\s+{_LABEL_PHRASE}(?:\s+recommendations?)?\s+"
    r"(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?"
    r"(?:(?:currently|clearly|obviously)\s+)?(?:recommended|suggested|proposed|justified|warranted)\b"
    r"|\bno\s+(?:(?:additional|new|further)\s+)?label\s+recommendations?\s+"
    r"(?:(?:is|are|was|were|has\s+been|have\s+been)\s+)?"
    r"(?:(?:currently|now)\s+)?(?:being\s+)?made\b"
    rf"|\bno\s+{_LABEL_PHRASE}\s+to\s+(?:recommend|suggest)\b"
    r"|\bno\s+(?:(?:additional|new|further)\s+)?label\s+recommendations?(?=\s*(?:[.,;!?]|$))"
    r"|\bno\s+(?:recommended|suggested)\s+labels?\b"
    r"|\b(?:label\s+recommendations?|recommended\s+labels?)\s*:\s*none\b"
    rf"|\bnone\s+of\s+(?:the\s+)?{_LABEL_PHRASE}\s+(?:is|are|was|were)\s+(?:recommended|suggested)\b"
    rf"|\b(?:i|we)\s+(?:(?:do|did|would|can|could|am|are|have)\s+not|cannot|"
    rf"don['’]t|didn['’]t|can['’]t|haven['’]t)\s+(?:recommend(?:ed|ing)?|suggest(?:ed|ing)?)\s+(?:any\s+)?{_LABEL_PHRASE}\b"
    rf"|\b(?:i|we)\s+(?:recommend|suggest)\s+no\s+{_LABEL_PHRASE}\b",
    re.IGNORECASE,
)
LABEL_CLAIM_CONTEXT = re.compile(
    r"\b(?:if|when|unless|whether|not\s+(?:saying|claiming)\s+(?:that\s+)?|"
    r"not\s+(?:true|accurate|correct)\s+that|"
    r"(?:reporter|maintainer|author)\s+(?:said|says|reported|reports)\s+(?:that\s+)?)\s*$",
    re.IGNORECASE,
)
LABEL_EXCEPTION = re.compile(r"\b(?:beyond|except(?:\s+for)?|other\s+than|apart\s+from)\b", re.IGNORECASE)


def label_consistency_fields(content: AnalysisContent) -> list[str]:
    """Find known zero-recommendation claims conflicting with a nonempty label list."""
    if not content.label_recommendations:
        return []
    names = [item.name for item in content.label_recommendations]
    fields = []
    for field, text in _analysis_texts(content):
        for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
            for claim in NO_LABEL_RECOMMENDATIONS.finditer(sentence):
                if LABEL_CLAIM_CONTEXT.search(sentence[:claim.start()]):
                    continue
                tail = sentence[claim.end():]
                if re.match(r"\s+by\s+(?:the\s+)?(?:reporter|maintainer|author)\b", tail, re.IGNORECASE):
                    continue
                exception = LABEL_EXCEPTION.search(sentence)
                if exception:
                    end = claim.start() if exception.start() < claim.start() else len(sentence)
                    exception_text = sentence[exception.end():end]
                    if all(
                        re.search(rf"(?<![\w:./-]){re.escape(name)}(?![\w:./-])", exception_text, re.IGNORECASE)
                        for name in names
                    ):
                        continue
                fields.append(field)
                break
            if field in fields:
                break
    return fields


def reproduction_claim_fields(content: AnalysisContent) -> list[str]:
    """Find output fields needing attribution; do not edit or inspect source excerpts."""
    return [field for field, text in _analysis_texts(content) if REPRODUCTION_CLAIM.search(text)]


# These are narrow known English claims, not a general semantic entailment check.
COMMENT_ABSENCE = re.compile(
    r"\b(?:no|zero)\s+(?:(?:discussion|issue|additional|further|other)\s+){0,3}comments?"
    r"(?:\s+(?:(?:are|were|is|was|have\s+been)\s+)?"
    r"(?:available|present|provided|posted|found|on\s+record|exist(?:s|ed)?)\b|(?=[.!?;:]|$))"
    r"|\b(?:issue|thread|discussion)\s+(?:has|had|contains|contained)\s+no\s+comments?\b"
    r"|\bcomments?\s+(?:are|were)\s+(?:absent|unavailable)\b",
    re.IGNORECASE,
)
ALL_COMMENTS_READ = re.compile(
    r"\b(?:all|every)\s+(?:the\s+)?(?:issue\s+)?comments?\s+"
    r"(?:(?:have|has)\s+been\s+|(?:were|was)\s+)(?:read|reviewed|checked|retrieved)\b"
    r"|\b(?:read|reviewed|checked|retrieved)\s+all\s+(?:the\s+)?(?:issue\s+)?comments?\b",
    re.IGNORECASE,
)
COMMENT_CLAIM_NEGATION = re.compile(
    r"\b(?:not|never|haven['’]t|hasn['’]t|didn['’]t|couldn['’]t)(?:\s+(?:yet|fully|actually))?\s*$",
    re.IGNORECASE,
)


def _positive_comment_claim(pattern: re.Pattern[str], text: str) -> bool:
    return any(
        not COMMENT_CLAIM_NEGATION.search(text[max(0, match.start() - 60):match.start()])
        for match in pattern.finditer(text)
    )


def comment_claim_fields(
    content: AnalysisContent, check: CommentCheck, *, visible_source_ids: set[str] | None = None,
) -> list[str]:
    """Reject known absence/completeness claims unsupported by retrieval state."""
    # Zero in target metadata supports a scoped count observation without forcing
    # an unnecessary lookup; its retrieval status still remains not_read.
    supports_absence = check.status == "empty" or (check.status == "not_read" and check.reported_count == 0)
    supports_complete_read = check.complete and (
        visible_source_ids is None or set(check.source_ids) <= visible_source_ids
    )
    return [field for field, text in _analysis_texts(content) if (
        (not supports_absence and _positive_comment_claim(COMMENT_ABSENCE, text))
        or (not supports_complete_read and _positive_comment_claim(ALL_COMMENTS_READ, text))
    )]
