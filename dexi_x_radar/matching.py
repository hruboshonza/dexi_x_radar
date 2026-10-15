"""Which draft did the owner post, and what did they change?

X prefixes a reply with the @handles it answers, so those are stripped first. Then:
- one draft at >= EXACT similarity          -> "A"
- sentences taken from two or more drafts   -> "merged:AB"
- one draft at >= EDITED similarity         -> "B~" (edited)
- anything else                             -> "own"
"""
import re
from difflib import SequenceMatcher

EXACT = 0.9
EDITED = 0.6
SENTENCE = 0.85

_LEADING_MENTIONS = re.compile(r"^(?:@\w+\s+)+")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def strip_mentions(text: str) -> str:
    return _LEADING_MENTIONS.sub("", text.strip())


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def word_diff(before: str, after: str) -> dict:
    a, b = before.split(), after.split()
    added, removed = [], []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if op in ("replace", "delete"):
            removed.append(" ".join(a[i1:i2]))
        if op in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
    return {"added": added, "removed": removed}


def match_reply(final_text: str, drafts: list[dict]) -> dict:
    """`drafts`: [{"label": "A", "text": ...}]. Returns matched, similarity, diff."""
    final = strip_mentions(final_text)
    if not drafts:
        return {"matched": "own", "similarity": 0.0, "diff": None}

    scored = sorted(((_ratio(final, d["text"]), d) for d in drafts), key=lambda x: -x[0])
    best_ratio, best = scored[0]
    if best_ratio >= EXACT:
        return {"matched": best["label"], "similarity": round(best_ratio, 3), "diff": word_diff(best["text"], final)}

    used, covered = set(), 0
    final_sentences = _sentences(final)
    for sentence in final_sentences:
        hits = [(r, d["label"]) for d in drafts for s in _sentences(d["text"]) if (r := _ratio(sentence, s)) >= SENTENCE]
        if hits:
            used.add(max(hits)[1])
            covered += 1
    if len(used) >= 2 and covered * 2 >= len(final_sentences):
        labels = "".join(sorted(used))
        source = " ".join(d["text"] for d in drafts if d["label"] in used)
        return {"matched": f"merged:{labels}", "similarity": round(_ratio(final, source), 3),
                "diff": word_diff(source, final)}

    if best_ratio >= EDITED:
        return {"matched": f"{best['label']}~", "similarity": round(best_ratio, 3),
                "diff": word_diff(best["text"], final)}
    return {"matched": "own", "similarity": round(best_ratio, 3), "diff": word_diff(best["text"], final)}
