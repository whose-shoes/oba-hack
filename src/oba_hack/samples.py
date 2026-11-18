"""데모 통화 샘플 (samples/*.txt).

형식: 1줄 '# 제목', 이어서 머리줄 'caller:', 'summary:'(에이닷 통화 요약), 'todos:'(에이닷 할일, ' | '로 구분),
그다음부터 화자 표기된 통화 내용.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).parent / "samples"
HEADER = re.compile(r"^(caller|summary|todos)\s*:\s*(.*)$")


@lru_cache
def all_samples() -> tuple[dict, ...]:
    out = []
    for p in sorted(DIR.glob("*.txt")):
        lines = p.read_text().splitlines()
        meta = {"caller": "엄마", "summary": "", "todos": ""}
        i = 1
        while i < len(lines) and (m := HEADER.match(lines[i].strip())):
            meta[m.group(1)] = m.group(2).strip()
            i += 1
        out.append({
            "id": p.stem, "title": lines[0].lstrip("# ").strip(), "caller": meta["caller"],
            "summary": meta["summary"], "todos": [t.strip() for t in meta["todos"].split("|") if t.strip()],
            "transcript": "\n".join(lines[i:]).strip(),
        })
    return tuple(out)


def get(sample_id: str) -> dict | None:
    return next((s for s in all_samples() if s["id"] == sample_id), None)
