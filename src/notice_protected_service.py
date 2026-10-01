"""Preserve source syntax during targeted retries of page-scoped notices."""
from __future__ import annotations

import re

from .master_workflow import TOKEN
from .notice_workflow import NUMBER, validate_notice

PREFIX = "⟪IP_KEEP_"
MARKER = re.compile(r"⟪IP_KEEP_[0-9]+⟫")
URL = r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"
PROTECTED = re.compile("(?:" + TOKEN.pattern + ")|" + URL +
    r"|[0-9０-９]+(?:[/：:／.,，．\-－][0-9０-９]+)+|" + NUMBER.pattern +
    r"|\r\n|\r|\n|\\n|[<>]")


def protect(source: str) -> tuple[str, dict[str, str]]:
    if PREFIX in source:
        raise ValueError("Notice source conflicts with reserved protection markers")
    literals = {}
    def substitute(match):
        marker = f"{PREFIX}{len(literals):04d}⟫"
        literals[marker] = match.group()
        return marker
    return PROTECTED.sub(substitute, source), literals


class ProtectedNoticeService:
    """Wrap the configured API client; validate restored text against real source.

    No cache or cross-page memory is introduced. Notice.run retains its ordinary
    page/source identities, source checksum checks and resumable result log.
    """
    def __init__(self, inner):
        self.inner = inner

    def translate_batch(self, lines, glossary):
        requests, guards = [], {}
        for line in lines:
            masked, literals = protect(line["source"])
            requests.append({**line, "source": masked})
            guards[line["id"]] = literals
        result = self.inner.translate_batch(requests, glossary)
        if set(result) != {line["id"] for line in lines}:
            raise ValueError("Protected notice response has missing or unexpected IDs")
        restored = {}
        for line in lines:
            target = result[line["id"]]
            literals = guards[line["id"]]
            if not isinstance(target, str) or MARKER.findall(target) != list(literals):
                raise ValueError("Protected notice markers changed, duplicated or reordered")
            target = MARKER.sub(lambda match: literals[match.group()], target)
            if PREFIX in target:
                raise ValueError("Unrestored notice marker")
            validate_notice(line["source"], target)
            restored[line["id"]] = target
        return restored
