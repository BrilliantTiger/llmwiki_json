#!/usr/bin/env python3
"""llmwiki_json CLI.

JSON canonical knowledge wiki: ingest, deterministic projection (build),
direct page/block get, md/html render, lint and append-only log.

Determinism contract
--------------------
`build` is a pure function of (wiki pages, groups.json). groups.json is read
from the data's config (`tools/config/` in a single repo, `config/` in a data
repo with `llmwiki.lock`), falling back to the code repo's default.
Running it twice on the same input produces byte-identical artifacts:
every collection is emitted in a stable sort order, every path stored in
an artifact is repo-relative, and layout coordinates are rounded. The
search index (`index/search.sqlite`, built by `llmwiki_index`) is published
by rewriting a fresh file in fixed DDL/PK order, so its bytes are deterministic too; `revision.json`
additionally records `search_root`, the sha256 of the published index file.

`build --changed PATH...` is incremental: only the hinted files are re-hashed,
the search index gets those pages swapped in place (`index/search.work.sqlite`
is the mutable work copy, WAL) and the JSON artifacts are projected from the
published index. An mtime scan verifies the hint; any change outside it falls
back to a full build. Incremental and full builds produce the same bytes.

Test hooks
----------
LLMWIKI_ROOT  repository root override (tests point this at a temp dir)
LLMWIKI_NOW   frozen clock, `YYYY-MM-DD` or a full ISO-8601 timestamp
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

try:  # package import (bench: `from scripts import llmwiki`)
    from . import llmwiki_text as T
    from . import llmwiki_index as search_index
    from . import llmwiki_paths as P
    from . import llmwiki_migrate as M
    from . import llmwiki_repo as R
except ImportError:  # run as a script or loaded from its file path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import llmwiki_text as T  # type: ignore[no-redef]
    import llmwiki_index as search_index  # type: ignore[no-redef]
    import llmwiki_paths as P  # type: ignore[no-redef]
    import llmwiki_migrate as M  # type: ignore[no-redef]
    import llmwiki_repo as R  # type: ignore[no-redef]

DEFAULT_ROOT = P.CODE_ROOT
ENV_ROOT = P.ENV_ROOT
SCHEMA_VERSION = P.SCHEMA_VERSION      # 정본 page 의 판 — llmwiki_paths 한 곳에만 적는다
ARTIFACT_VERSION = "1.0"               # map/graph/revision 같은 파생물의 형식 판 (page 스키마와 따로 간다)
ENV_NOW = "LLMWIKI_NOW"
SEARCH_INDEX = search_index.DB_NAME

ALLOWED_TYPES = {"source", "entity", "concept", "synthesis", "project", "home", "index", "log"}
ALLOWED_BLOCKS = {"heading", "paragraph", "list", "table", "quote", "conflict", "current",
                  "open_question", "code", "thematic_break", "markdown", "raw"}
ALLOWED_LINK_KINDS = {"wiki", "source", "supersedes", "related"}
UNLINKED_TYPES = {"home", "index", "log"}
PAGE_DIRS = {
    "source": "sources", "entity": "entities", "concept": "concepts",
    "synthesis": "syntheses", "project": "projects",
    "home": "", "index": "", "log": "",
}

WIKILINK = T.WIKILINK
LINK_SEPARATOR = T.LINK_SEPARATOR
SECRET = T.SECRET
# YAML subset used by frontmatter: `key: value`, `- item`, `[a, "b, c"]`.
META_KEY = re.compile(r"^([^\s:#][^:]*):[ \t]*(.*)$")
BLOCK_ITEM = re.compile(r"^[ \t]+-[ \t]*(.*)$")
FLOW_ITEM = re.compile(r"""[ \t]*(?:"([^"]*)"|'([^']*)'|([^,]*))[ \t]*(?:,|$)""")
SOURCE_REF = re.compile(r"^(?:page|source|raw):\S+$|^user:\d{4}-\d{2}-\d{2}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BLOCK_KEEP = re.compile(r"[^a-zA-Z0-9가-힣._-]+")

CONFLICT_MARK = "⚠️ 상충"
CURRENT_MARK = "✅ 현행"
# 아직 답이 없는 질문: `> ❓ 미결: …` 인용에 `owner:`/`due:`(또는 `담당:`/`기한:`) 줄을 둔다. 표지는 낱말까지다 —
# `이게 맞나요❓` 같은 인용은 질문이 아니다. owner·due 는 그 줄 안의 값이다(빈 `owner:` 가 다음 줄을 먹지 않는다).
OPEN_MARK = "❓ 미결"
OPEN_OWNER = re.compile(r"(?mi)^[ \t]*(?:owner|담당)[ \t]*:[ \t]*(\S.*?)[ \t]*$")
OPEN_DUE = re.compile(r"(?mi)^[ \t]*(?:due|기한)[ \t]*:[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*$")

# Deterministic radial layout. Project groups occupy adjacent angular sectors,
# while every sector fills the same disk so the complete graph stays circular.
RESERVED_PROJECT_GROUPS = {"multi", "ungrouped"}
AUTO_PROJECT_COLORS = (
    "#65e79c", "#ff8dca", "#ffa56d", "#56e1f2", "#91a7ff", "#c69cff",
)
GOLDEN_FRACTION = 0.6180339887498949
LAYOUT_RADIUS = 18.0


# --------------------------------------------------------------------------- utils
norm = T.norm
sha = T.sha
sha_bytes = T.sha_bytes
canonical = T.canonical
redact = T.redact
link_key = T.link_key
refs_in = T.refs_in
strip_ref_prefix = T.strip_ref_prefix
reference_links = T.reference_links
implied_links = T.implied_links


def today() -> str:
    override = os.environ.get(ENV_NOW)
    return override[:10] if override else dt.date.today().isoformat()


def timestamp() -> str:
    override = os.environ.get(ENV_NOW)
    if not override:
        return dt.datetime.now(dt.timezone.utc).isoformat()
    return override if "T" in override else f"{override[:10]}T00:00:00+00:00"


def dump(path: Path, data: Any, *, pretty: bool = False) -> None:
    """Atomic, newline-terminated, deterministic JSON write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":")) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def safe_name(page_id: str) -> str:
    """Filesystem-safe shard name; never escapes its directory."""
    stem = BLOCK_KEEP.sub("-", page_id.removeprefix("page:")).strip("-. ") or sha(page_id, 12)
    return stem + ".json"


class WikiError(Exception):
    """User-facing failure; the CLI turns this into exit code 2."""


# --------------------------------------------------------------------------- workspace
class Workspace:
    """Resolves every path the CLI touches, so tests can relocate the tree."""

    def __init__(self, root: str | os.PathLike[str] | None = None, fixtures: bool = False) -> None:
        self.root = P.resolve_root(root)
        self.layout = P.Layout(self.root)
        self.fixtures = bool(fixtures)

    @property
    def split(self) -> bool:
        return self.layout.split

    @property
    def knowledge(self) -> Path:
        return self.layout.wiki

    @property
    def fixtures_dir(self) -> Path:
        return self.layout.fixtures

    @property
    def pages_dir(self) -> Path:
        return self.fixtures_dir if self.fixtures else self.knowledge

    @property
    def raw(self) -> Path:
        return self.layout.raw

    @property
    def index(self) -> Path:
        return self.layout.index

    @property
    def search_db(self) -> Path:
        return self.index / SEARCH_INDEX

    @property
    def work_db(self) -> Path:
        return self.index / search_index.WORK_NAME

    @property
    def state_path(self) -> Path:
        return self.index / search_index.STATE_NAME

    @property
    def public(self) -> Path:
        return self.layout.viewer_data

    @property
    def groups_path(self) -> Path:
        """읽을 groups.json — 데이터 쪽에 없으면 코드의 기본값."""
        return self.layout.config("groups.json")

    @property
    def groups_write_path(self) -> Path:
        """ingest 가 새 프로젝트 그룹을 적는 자리 — 언제나 데이터 쪽이다."""
        return self.layout.config_dir / "groups.json"

    @property
    def tags_path(self) -> Path:
        return self.layout.config("tags.json")

    @property
    def schema_path(self) -> Path:
        return self.layout.schema

    @property
    def log_path(self) -> Path:
        return self.layout.wiki / "log.jsonl"

    def rel(self, path: Path) -> str:
        """Repo-relative POSIX path when possible — artifacts must not embed $HOME."""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def load_documents(self, source: Path | None = None) -> list[tuple[str, dict[str, Any]]]:
        """(relative shard path, page) pairs, sorted by page id. `source` 를 주면 그 디렉터리에서 읽는다."""
        docs: list[tuple[str, dict[str, Any]]] = []
        source = source or self.pages_dir
        if not source.exists():
            return docs
        for path in sorted(source.rglob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise WikiError(f"{self.rel(path)}: invalid JSON ({exc})") from exc
            for page in (value if isinstance(value, list) else [value]):
                docs.append((self.rel(path), page))
        docs.sort(key=lambda item: (str(item[1].get("id", "")), item[0]))
        return docs

    def load_pages(self) -> list[dict[str, Any]]:
        return [page for _, page in self.load_documents()]

    def load_groups(self) -> dict[str, Any]:
        if not self.groups_path.exists():
            raise WikiError(f"missing {self.rel(self.groups_path)}")
        return json.loads(self.groups_path.read_text(encoding="utf-8"))

    def load_tag_vocabulary(self) -> list[str] | None:
        """위키 설정의 `tags.json` 허용 tag (선택). 파일이 없으면 None — 어휘를 검사하지 않는다.
        분리 모드에서 데이터 쪽에 없으면 코드 repo 의 것을 빌려 쓰지 않는다 (위키마다 다른 어휘다)."""
        if not self.tags_path.exists():
            return None
        value = json.loads(self.tags_path.read_text(encoding="utf-8"))
        tags = value.get("tags") if isinstance(value, dict) else None
        if not isinstance(tags, list):
            raise WikiError(f"{self.rel(self.tags_path)}: {{\"tags\": [...]}} 이어야 한다")
        return string_list(tags)


# --------------------------------------------------------------------------- markdown parsing
def unquote(value: str) -> str:
    """Strip one matching pair of surrounding quotes — `"a, b"` stays one value."""
    value = value.strip()
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1].strip()
    return value


def split_flow(value: str) -> list[str]:
    """`a, "b, c", 'd'` -> ['a', 'b, c', 'd'] — commas inside quotes do not split."""
    items: list[str] = []
    position = 0
    while position < len(value):
        match = FLOW_ITEM.match(value, position)
        if not match or match.end() == position:
            break
        items.append(norm(next(g for g in match.groups() if g is not None)))
        position = match.end()
    return [item for item in items if item]


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return split_flow(value[1:-1])
    if value in {"null", "~", ""}:
        return None
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return unquote(value)


def string_list(value: Any) -> list[str]:
    """Coerce a frontmatter value to a clean, de-duplicated list of strings.

    `tags: [a, b]`, a block sequence, a bare `tags: a` and `tags: a, b` all
    have to land on the same shape: the schema demands an array of unique
    strings, and a page whose tags silently arrived as `None` is a page the
    graph cannot colour or filter.
    """
    if value is None or value is False or value == "":
        return []
    if isinstance(value, str):
        items: list[Any] = split_flow(value) if "," in value else [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return list(dict.fromkeys(text for text in (norm(item) for item in items) if text))


# --------------------------------------------------------------------------- tags
TAG_SEPARATORS = re.compile(r"[\s_.\-/]+")


def tag_key(tag: Any) -> str:
    """`pre_v0_1-1`, `pre-v0.1-1`, `Pre V0.1 1` 은 한 tag 다 — 대소문자·`_ . - /`·공백을 가리지 않는다."""
    text = unicodedata.normalize("NFKC", norm(tag)).casefold()
    return TAG_SEPARATORS.sub("-", text).strip("-")


def tag_spellings(pages: Iterable[dict[str, Any]], vocabulary: Iterable[str] | None = None) -> dict[str, str]:
    """tag key → 쓸 표기. 허용 목록(`tools/config/tags.json`) 에 있으면 그 표기, 없으면 정본에서 가장 많이
    쓴 표기(같은 수면 사전순 앞)."""
    counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for page in pages:
        for tag in string_list(page.get("tags")):
            counts[tag_key(tag)][tag] += 1
    spellings = {key: min(seen, key=lambda tag: (-seen[tag], tag)) for key, seen in counts.items() if key}
    for tag in vocabulary or ():
        spellings[tag_key(tag)] = tag
    return spellings


def canonical_tags(tags: Any, spellings: dict[str, str]) -> list[str]:
    """변형을 한 표기로 모으고 같은 key 는 한 번만 둔다 — 처음 나온 순서대로."""
    out: list[str] = []
    seen: set[str] = set()
    for tag in string_list(tags):
        key = tag_key(tag)
        if key and key not in seen:
            seen.add(key)
            out.append(spellings.get(key, tag))
    return out


BLOCK_SCALAR = frozenset({">", "|", ">-", "|-", ">+", "|+"})


def frontmatter_scalar(text: str, key: str) -> str | None:
    """One frontmatter value as written — not read as a flow list (`summary: [초안] 정책 [v2]`),
    and a `>`/`|` block scalar gathers its indented lines."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    lines = text[4:end].splitlines()
    for number, line in enumerate(lines):
        entry = META_KEY.match(line)
        if not entry or norm(entry.group(1)) != key:
            continue
        value = entry.group(2).strip()
        if value not in BLOCK_SCALAR:
            return unquote(value)
        gathered = []
        for following in lines[number + 1:]:
            if following.strip() and not following[:1].isspace():
                break
            gathered.append(following.strip())
        return " ".join(part for part in gathered if part)
    return None


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse the YAML subset real notes use: scalars, flow lists, block sequences.

    A block sequence (`tags:` then indented `- value` lines) is what most
    editors and humans write. Dropping it silently used to leave `tags`,
    `projects` and `sources` empty, so pages arrived unclassified.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    meta: dict[str, Any] = {}
    key: str | None = None
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = BLOCK_ITEM.match(line)
        if item and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            value = unquote(norm(item.group(1)))
            if value:
                meta[key].append(value)
            continue
        entry = META_KEY.match(line)
        if not entry:
            continue
        key = norm(entry.group(1))
        meta[key] = parse_scalar(entry.group(2))
    return meta, text[end + 5:]


def block_fingerprint(kind: str, raw: str) -> str:
    return sha(kind + "\0" + re.sub(r"\s+", " ", raw).strip(), 16)


def block_id_of(page_id: str, fingerprint: str, occurrence: int) -> str:
    return f"block:{page_id.removeprefix('page:')}:{fingerprint}:{occurrence}"


def make_block(page_id: str, kind: str, raw: str, data: dict[str, Any], occurrence: int,
               resolution: dict[str, Any] | None = None) -> dict[str, Any]:
    """Content-addressed block id: stable across edits elsewhere on the page."""
    fingerprint = block_fingerprint(kind, raw)
    block: dict[str, Any] = {
        "id": block_id_of(page_id, fingerprint, occurrence),
        "kind": kind, "data": data, "refs": refs_in(raw),
        "source_text": raw, "fingerprint": fingerprint,
    }
    if resolution:
        block["resolution"] = resolution
    return block


def list_items(raw_lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in raw_lines:
        marker = re.match(r"^\s*(?:[-*+]|\d+\.)\s+(.*)$", line)
        if marker:
            items.append(marker.group(1).strip())
        elif items and line.strip():
            items[-1] = f"{items[-1]} {line.strip()}"
    return items


TABLE_PIPE = re.compile(r"(?<!\\)\|")


def table_cells(row: str) -> list[str]:
    """`| a | str \\| None |` → ['a', 'str | None'] — GFM 처럼 `\\|` 는 칸을 나누지 않고 칸 안의 `|` 다."""
    row = row.strip().lstrip("|")
    while row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [cell.strip().replace("\\|", "|") for cell in TABLE_PIPE.split(row)]


def legacy_table_rows(source_text: str) -> list[list[str]]:
    """2026-10 까지의 파서가 읽은 표 — 칸을 먼저 `|` 로 나눈 뒤 `\\|` 를 풀어서 `\\|` 가 칸을 갈랐다."""
    return [[c.strip().replace("\\|", "|") for c in r.strip().strip("|").split("|")] for r in source_text.split("\n")]


def parse_blocks(page_id: str, body: str) -> tuple[dict[str, Any], list[str]]:
    lines = body.splitlines()
    blocks: dict[str, Any] = {}
    order: list[str] = []
    occurrences: Counter[tuple[str, str]] = Counter()

    def add(kind: str, raw_lines: list[str], data: dict[str, Any],
            resolution: dict[str, Any] | None = None) -> None:
        raw = "\n".join(raw_lines)
        key = (kind, block_fingerprint(kind, raw))
        occurrences[key] += 1
        block = make_block(page_id, kind, raw, data, occurrences[key], resolution)
        blocks[block["id"]] = block
        order.append(block["id"])

    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        if line.startswith("```"):
            raw = [line]
            language = line[3:].strip()
            i += 1
            while i < len(lines):
                raw.append(lines[i])
                closed = lines[i].startswith("```")
                i += 1
                if closed:
                    break
            closed = len(raw) > 1 and raw[-1].startswith("```")
            while not closed and len(raw) > 1 and not raw[-1].strip():
                raw.pop()       # 닫히지 않은 fence 는 page 끝까지 간다 — 끝의 빈 줄은 내용이 아니고, 남기면 다시 읽을 때 사라진다
            inner = raw[1:-1] if closed else raw[1:]
            add("code", raw, {"language": language, "text": "\n".join(inner)})
            continue

        heading = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading:
            add("heading", [line], {"level": len(heading.group(1)), "text": heading.group(2).strip()})
            i += 1
            continue

        if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
            add("thematic_break", [line], {})
            i += 1
            continue

        if line.lstrip().startswith(">"):
            raw = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                raw.append(lines[i])
                i += 1
            text = "\n".join(re.sub(r"^\s*>\s?", "", x) for x in raw)
            kind = "conflict" if CONFLICT_MARK in text else "current" if CURRENT_MARK in text \
                else "open_question" if OPEN_MARK in text else "quote"
            resolution = None
            data: dict[str, Any] = {"text": text}
            if kind == "conflict":
                resolution = {"status": "unresolved"}
            elif kind == "current":
                resolution = {"status": "resolved", "decided_at": today()}
            elif kind == "open_question":
                owner, due = OPEN_OWNER.search(text), OPEN_DUE.search(text)
                data.update(owner=owner.group(1) if owner else "", due=due.group(1) if due else None)
            add(kind, raw, data, resolution)
            continue

        if line.startswith("|"):
            raw = []
            while i < len(lines) and lines[i].startswith("|"):
                raw.append(lines[i])
                i += 1
            rows = [table_cells(r) for r in raw]
            add("table", raw, {"rows": rows})
            continue

        if re.match(r"^\s*(?:[-*+] |\d+\. )", line):
            raw = []
            while i < len(lines) and (re.match(r"^\s*(?:[-*+] |\d+\. )", lines[i])
                                      or (lines[i].startswith("  ") and lines[i].strip())):
                raw.append(lines[i])
                i += 1
            add("list", raw, {"ordered": bool(re.match(r"^\s*\d+\. ", raw[0])),
                              "items": list_items(raw), "text": "\n".join(raw)})
            continue

        raw = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not re.match(
                r"^(#{1,6})\s|^```|^\||^\s*>|^\s*(?:[-*+] |\d+\. )", lines[i]):
            raw.append(lines[i])
            i += 1
        add("paragraph", raw, {"text": "\n".join(raw)})
    return blocks, order


def page_links(page_id: str, blocks: dict[str, Any], order: list[str]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for bid in order:
        for match in WIKILINK.finditer(blocks[bid]["source_text"]):
            if not link_key(match.group(1)):            # `[[ ]]` 는 아무 page 도 가리키지 않는다
                continue
            links.append({"target": norm(match.group(1)),
                          "label": norm(match.group(3)) or norm(match.group(1)),
                          "anchor": norm(match.group(2)), "kind": "wiki", "block_id": bid})
    return links


def page_lookup(pages: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Resolve a link target by slug, id or title — slugs always win a tie."""
    pages = list(pages)
    lookup: dict[str, dict[str, Any]] = {}
    for page in pages:
        for value in (page.get("slug", ""), page.get("id", "")):
            key = link_key(value)
            if key:
                lookup.setdefault(key, page)
    for page in pages:
        key = link_key(page.get("title", ""))
        if key:
            lookup.setdefault(key, page)
    return lookup


def summary_from_blocks(blocks: dict[str, Any], order: list[str]) -> str:
    """Return a compact searchable lead even when a page starts with a list/quote.

    A table flattens into `항목 값 --- --- …`, so it is the lead only when the
    page has no other body block.
    """
    tables: list[str] = []
    for bid in order:
        block = blocks[bid]
        if block["kind"] in {"heading", "thematic_break", "code"}:
            continue
        if block["kind"] == "table":
            tables.append(bid)
            continue
        text = lead_text(block)
        if text:
            return clip_lead(text)
    for bid in tables:
        text = lead_text(blocks[bid])
        if text:
            return clip_lead(text)
    return ""


def clip_lead(text: str, limit: int = 280) -> str:
    """낱말 가운데서 자르지 않는다 — `<your-password>` 가 `<yo` 로 끊기면 깨진 자리표시가 비밀처럼 읽힌다."""
    if len(text) <= limit:
        return text
    space = text.rfind(" ", 0, limit + 1)
    return (text[:space] if space > 0 else text[:limit]).rstrip()


def lead_text(block: dict[str, Any]) -> str:
    text = norm(block["data"].get("text") or block["source_text"])
    text = WIKILINK.sub(lambda match: norm(match.group(3)) or norm(match.group(1)), text)
    text = re.sub(r"(?m)^\s*(?:[-*+]|\d+\.)\s+", "", text)
    # `_` 와 `<>` 는 남긴다 — value_token 같은 식별자와 `<your token>` 자리표시가 요약에서 깨지면
    # 다른 낱말이 되고, 깨진 자리표시는 비밀처럼 읽힌다.
    text = re.sub(r"[*`#|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def is_page_object(path: Path) -> bool:
    """True only for a JSON file that is itself a canonical page.

    Ordinary data/config JSON in raw/ (skills-lock.json, settings.json, data
    samples) is content to be recorded, not a page to be validated.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False
    def looks_like_page(value: Any) -> bool:
        return isinstance(value, dict) and "schema_version" in value and "blocks" in value

    # A list of pages still reaches the page branch so it keeps raising the
    # "single page object" error rather than being silently stored as data.
    if isinstance(data, list):
        return bool(data) and looks_like_page(data[0])
    return looks_like_page(data)


def load_extractors():
    """Import scripts/extract.py regardless of how llmwiki.py was invoked."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "llmwiki_extract", Path(__file__).resolve().parent / "extract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stub_body(ws: Workspace, path: Path) -> str:
    """Body for a source that carries no extractable text (image, archive, media).

    The file still becomes a page so that raw/ and wiki/ stay one-to-one.
    """
    stat = path.stat()
    kb = max(1, round(stat.st_size / 1024))
    return "\n".join([
        f"# {path.name}",
        "",
        f"본문을 추출할 수 없는 원본입니다 (`{path.suffix.lower() or 'no-ext'}`). "
        "이 페이지는 파일의 존재와 위치를 기록합니다.",
        "",
        f"- 경로: `{ws.rel(path)}`",
        f"- 형식: `{path.suffix.lower().lstrip('.') or 'unknown'}`",
        f"- 크기: {kb:,} KB",
        f"- 수정: {dt.datetime.fromtimestamp(stat.st_mtime, dt.timezone.utc).date().isoformat()}",
    ])


def redact_secrets(text: str) -> tuple[str, int]:
    """Mask credential values in *converted* bodies (pdf/office/…).

    An original markdown source still fails hard — it can be edited. A binary
    original cannot, so the page records the document with the value removed
    instead of dropping the document entirely.
    """
    masked = T.redact(text)
    return masked, max(0, masked.count(T.MASK) - text.count(T.MASK))


def page_from_markdown(ws: Workspace, path: Path, page_type: str | None = None,
                       projects: list[str] | None = None, summary: str = "",
                       slug: str = "", text: str = "", fmt: str = "markdown",
                       tags: list[str] | None = None) -> dict[str, Any]:
    # `text` lets a caller pass a converted body (PDF/office/…); the page still
    # records the untouched original as its raw_ref.
    text = text or path.read_text(encoding="utf-8")
    assert_no_secrets(text, ws.rel(path))
    meta, body = split_frontmatter(text)
    slug = norm(slug or path.stem).replace(" ", "-").lower()
    page_id = f"page:{slug}"
    blocks, order = parse_blocks(page_id, body)
    title = next((b["data"]["text"] for b in blocks.values()
                  if b["kind"] == "heading" and b["data"].get("level") == 1), slug)
    source_refs = [value if SOURCE_REF.match(value) else f"source:{value}"
                   for value in string_list(meta.get("sources"))]
    raw_ref = norm(meta.get("raw")) or repo_path(ws, path)
    meta_summary = frontmatter_scalar(text, "summary")
    inferred_summary = summary_from_blocks(blocks, order)
    stamp = today()
    links = page_links(page_id, blocks, order)
    links.extend(reference_links(source_refs, string_list(meta.get("supersedes")),
                                 string_list(meta.get("related"))))
    vocabulary = {tag_key(tag): tag for tag in ws.load_tag_vocabulary() or ()}
    keywords = string_list(meta.get("keywords"))
    return {
        "schema_version": SCHEMA_VERSION, "id": page_id, "slug": slug, "title": title,
        "type": page_type or meta.get("type") or "source",
        "created": meta.get("created") or stamp, "updated": meta.get("updated") or stamp,
        "tags": canonical_tags(string_list(tags) or meta.get("tags"), vocabulary),
        **({"keywords": keywords} if keywords else {}),
        "projects": string_list(projects) or string_list(meta.get("projects")),
        "sources": source_refs, "raw_ref": raw_ref,
        "summary": summary or norm(meta_summary) or inferred_summary[:280],
        "blocks": blocks, "block_order": order, "links": links,
        "history": [{"at": stamp, "action": "ingested", "actor": "llmwiki-cli",
                     "note": repo_path(ws, path) or path.name}],
        "source_snapshot": {"format": fmt, "text": text, "sha256": sha(text)},
    }


def repo_path(ws: Workspace, path: Path) -> str | None:
    """Repo-relative path, or None for a file outside the repository (a draft in a temp dir)
    — a machine-local absolute path means nothing in another clone."""
    rel = ws.rel(path)
    return None if Path(rel).is_absolute() else rel


# --------------------------------------------------------------------------- schema validation
class SchemaValidator:
    """Small JSON Schema subset covering tools/schema/page.schema.json.

    Supported: $ref/$defs, type, const, enum, pattern, minLength, format:date,
    required, properties, additionalProperties, items, uniqueItems.
    Keeps schema and code in sync without adding a runtime dependency.
    """

    TYPES = {"object": dict, "array": list, "string": str, "boolean": bool,
             "number": (int, float), "integer": int, "null": type(None)}

    def __init__(self, schema: dict[str, Any]) -> None:
        self.schema = schema

    @classmethod
    def load(cls, ws: Workspace) -> "SchemaValidator | None":
        if not ws.schema_path.exists():
            return None
        return cls(json.loads(ws.schema_path.read_text(encoding="utf-8")))

    def resolve(self, schema: dict[str, Any]) -> dict[str, Any]:
        ref = schema.get("$ref")
        if not ref:
            return schema
        node: Any = self.schema
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return self.resolve(node)

    def validate(self, value: Any, schema: dict[str, Any] | None = None,
                 path: str = "") -> list[str]:
        schema = self.resolve(schema if schema is not None else self.schema)
        errors: list[str] = []
        where = path or "/"

        if "const" in schema and value != schema["const"]:
            errors.append(f"{where}: expected {schema['const']!r}")
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} not in {schema['enum']}")

        expected = schema.get("type")
        if expected:
            wanted = expected if isinstance(expected, list) else [expected]
            types = tuple(self.TYPES[name] for name in wanted if name in self.TYPES)
            ok = isinstance(value, types) and not (isinstance(value, bool) and "boolean" not in wanted)
            if not ok:
                errors.append(f"{where}: expected type {'|'.join(wanted)}, got {type(value).__name__}")
                return errors

        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                errors.append(f"{where}: {value!r} does not match {schema['pattern']}")
            if "minLength" in schema and len(value) < schema["minLength"]:
                errors.append(f"{where}: shorter than {schema['minLength']}")
            if schema.get("format") == "date" and not DATE.fullmatch(value):
                errors.append(f"{where}: {value!r} is not a YYYY-MM-DD date")

        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    errors.append(f"{where}: missing required '{key}'")
            properties = schema.get("properties", {})
            extra = schema.get("additionalProperties", True)
            for key, item in value.items():
                if key in properties:
                    errors.extend(self.validate(item, properties[key], f"{path}/{key}"))
                elif extra is False:
                    errors.append(f"{where}: unexpected property '{key}'")
                elif isinstance(extra, dict):
                    errors.extend(self.validate(item, extra, f"{path}/{key}"))

        if isinstance(value, list):
            if "items" in schema:
                for idx, item in enumerate(value):
                    errors.extend(self.validate(item, schema["items"], f"{path}/{idx}"))
            if schema.get("uniqueItems") and len({canonical(v) for v in value}) != len(value):
                errors.append(f"{where}: items must be unique")
        return errors


def assert_no_secrets(text: str, label: str) -> None:
    hit = T.secret_hit(text)
    if hit:
        raise WikiError(
            f"{label}: 보안정보로 보이는 '{hit}' 값이 있습니다. "
            "원본을 정리하거나 '(접속 정보 생략)'으로 치환한 뒤 ingest 하세요.")


def validate_page(page: dict[str, Any], validator: SchemaValidator | None = None) -> list[str]:
    """Structural checks; `validator` adds full schema conformance when available."""
    pid = page.get("id", "?")
    errors: list[str] = []
    if not isinstance(page, dict):
        return [f"{pid}: page must be an object"]

    required = ["schema_version", "id", "slug", "title", "type", "created", "updated",
                "tags", "projects", "sources", "blocks", "block_order", "links", "history"]
    for key in required:
        if key not in page:
            errors.append(f"{pid}: missing {key}")
    if errors:
        return errors

    if page["schema_version"] != SCHEMA_VERSION:
        errors.append(f"{pid}: schema_version must be {SCHEMA_VERSION} (found {page['schema_version']!r}"
                      f" — `llmwiki migrate` 로 판을 맞춘다)")
    if not str(pid).startswith("page:"):
        errors.append(f"{pid}: invalid page id")
    if pid != f"page:{page['slug']}":
        errors.append(f"{pid}: id must be 'page:' + slug ({page['slug']!r})")
    if page["type"] not in ALLOWED_TYPES:
        errors.append(f"{pid}: invalid type {page['type']}")
    for key in ("created", "updated"):
        if not DATE.fullmatch(str(page[key])):
            errors.append(f"{pid}: {key} must be YYYY-MM-DD")
    for ref in page["sources"]:
        if not SOURCE_REF.match(str(ref)):
            errors.append(f"{pid}: invalid source ref {ref!r} (page:/source:/raw:/user:YYYY-MM-DD)")

    blocks, order = page["blocks"], page["block_order"]
    if not isinstance(blocks, dict) or not isinstance(order, list):
        return errors + [f"{pid}: blocks must be an object and block_order an array"]
    if len(order) != len(set(order)):
        errors.append(f"{pid}: duplicate ids in block_order")
    if set(order) != set(blocks):
        missing = sorted(set(blocks) - set(order))
        unknown = sorted(set(order) - set(blocks))
        errors.append(f"{pid}: block_order mismatch (unordered={missing}, unknown={unknown})")
    for bid, block in blocks.items():
        if block.get("id") != bid:
            errors.append(f"{pid}: block key/id mismatch {bid}")
        if block.get("kind") not in ALLOWED_BLOCKS:
            errors.append(f"{pid}: invalid block kind {block.get('kind')} on {bid}")
        for key in ("data", "refs", "source_text", "fingerprint"):
            if key not in block:
                errors.append(f"{pid}:{bid}: missing {key}")
        status = block.get("resolution", {}).get("status")
        if status is not None and status not in {"resolved", "unresolved"}:
            errors.append(f"{pid}:{bid}: invalid resolution status {status!r}")
        if block.get("kind") == "open_question":
            data = block.get("data") if isinstance(block.get("data"), dict) else {}
            if not isinstance(data.get("owner"), str):
                errors.append(f"{pid}:{bid}: open_question needs data.owner (string, '' when nobody owns it)")
            if "due" not in data or not (data["due"] is None or DATE.fullmatch(str(data["due"]))):
                errors.append(f"{pid}:{bid}: open_question needs data.due (YYYY-MM-DD or null)")
    aliases = page.get("block_aliases") or {}
    if isinstance(aliases, dict):
        for old, live in aliases.items():
            if old in blocks:
                errors.append(f"{pid}: block alias {old} is a live block id")
            if live not in blocks:
                errors.append(f"{pid}: block alias {old} points at unknown block {live}")
    for idx, link in enumerate(page["links"]):
        if link.get("kind") not in ALLOWED_LINK_KINDS:
            errors.append(f"{pid}: link[{idx}] invalid kind {link.get('kind')}")
        if not norm(link.get("target")):
            errors.append(f"{pid}: link[{idx}] empty target")
        if link.get("block_id") and link["block_id"] not in blocks:
            errors.append(f"{pid}: link[{idx}] unknown block_id {link['block_id']}")

    # build 가 page 를 통째로 viewer/public/data 로 복사한다 — 손으로 넣은 비밀도 여기서 막는다.
    errors.extend(f"{pid}: 보안정보로 보이는 '{label}' 값이 {where} 에 있다 — '(접속 정보 생략)'으로 치환한다"
                  for where, label in T.secret_paths(page))
    if validator:
        errors.extend(f"{pid}{err}" for err in validator.validate(page))
    return sorted(dict.fromkeys(errors))


# --------------------------------------------------------------------------- consistency
# source_text 가 block 의 정본이고 data·fingerprint·refs·id·page links 는 거기서 나온다. 손으로 고친
# JSON 은 이 사이가 어긋난다 — 검색은 data.text 를, 렌더와 그래프는 source_text·refs·links 를 봐서
# 같은 page 가 화면마다 다른 말을 한다. 이 kind 들의 data 는 본문 밖의 값(판정·주장 목록)을 담아도 된다.
DATA_FREE_KINDS = frozenset({"conflict", "current", "raw", "markdown"})
# 그중 인용에서 나온 kind — data.text 는 본문에서 나오고, 나머지 data 필드(주장 목록 같은 것)는 본문 밖이다.
QUOTED_FREE_KINDS = frozenset({"conflict", "current"})
QUOTE_KINDS = frozenset({"quote", "conflict", "current", "open_question"})


def parse_one(page_id: str, source_text: str) -> dict[str, Any] | None:
    """`source_text` 를 혼자 파싱한 block — block 하나로 읽히지 않으면 None."""
    blocks, order = parse_blocks(page_id, source_text)
    return blocks[order[0]] if len(order) == 1 else None


def block_drift(page: dict[str, Any], bid: str, block: dict[str, Any]) -> list[str]:
    """이 block 에서 source_text 와 어긋난 파생 필드들 (설명 문장 목록, 비면 일치)."""
    kind, text = block.get("kind"), block.get("source_text")
    if not isinstance(text, str) or not isinstance(kind, str):
        return []                                     # 구조 오류는 validate_page 가 낸다
    problems: list[str] = []
    fingerprint = block_fingerprint(kind, text)
    if block.get("fingerprint") != fingerprint:
        problems.append(f"fingerprint {block.get('fingerprint')!r} != block_fingerprint(kind, source_text) "
                        f"{fingerprint!r}")
    if kind not in DATA_FREE_KINDS:
        parsed = parse_one(page["id"], text)
        if parsed is None:
            problems.append("source_text does not parse to exactly one block")
        elif parsed["kind"] != kind:
            problems.append(f"source_text parses as {parsed['kind']}, not {kind}")
        elif parsed["data"] != block.get("data"):
            problems.append("data != parse(source_text)")
    if not isinstance(block.get("refs"), list) or set(block["refs"]) != set(refs_in(text)):
        problems.append(f"refs {block.get('refs')!r} != [[links]] in source_text {refs_in(text)!r}")
    if not re.fullmatch(re.escape(f"block:{page['id'].removeprefix('page:')}:") + r"[0-9a-f]{16}:[1-9][0-9]*", bid):
        problems.append("block id is not block:<slug>:<16hex>:<n>")
    elif not carries_fingerprint(page, bid, str(block.get("fingerprint"))):
        problems.append("block id does not carry its fingerprint")
    return problems


def body_links(page: dict[str, Any]) -> dict[str, str]:
    """본문 `[[링크]]` — link_key → 처음 적힌 표기."""
    out: dict[str, str] = {}
    blocks = page.get("blocks") or {}
    for bid in page.get("block_order") or []:
        for match in WIKILINK.finditer(str((blocks.get(bid) or {}).get("source_text") or "")):
            out.setdefault(link_key(match.group(1)), norm(match.group(1)))
    out.pop("", None)
    return out


def consistency_errors(page: dict[str, Any]) -> list[str]:
    """source_text 에서 다시 만든 값과 정본이 다른 곳. `validate`·`lint` 가 error 로 낸다."""
    pid = page.get("id", "?")
    blocks = page.get("blocks")
    if not isinstance(blocks, dict) or not isinstance(page.get("block_order"), list):
        return []
    errors = [f"{pid}:{bid}: {problem}"
              for bid in page["block_order"] if isinstance(blocks.get(bid), dict)
              for problem in block_drift(page, bid, blocks[bid])]
    body = body_links(page)
    declared: dict[str, str] = {}
    for link in page.get("links") or []:
        if isinstance(link, dict) and link.get("kind", "wiki") == "wiki" and link_key(str(link.get("target") or "")):
            declared.setdefault(link_key(str(link["target"])), norm(link["target"]))
    for key in sorted(set(body) - set(declared)):
        errors.append(f"{pid}: block link [[{body[key]}]] missing from links")
    for key in sorted(set(declared) - set(body)):
        errors.append(f"{pid}: wiki link [[{declared[key]}]] is not in the body")
    return errors


# --------------------------------------------------------------------------- projection / build
def project_group(projects: Iterable[str], groups: dict[str, Any]) -> str:
    projects = [norm(project).lower() for project in projects if norm(project)]
    if len(projects) > 1:
        return "multi"
    for key, config in groups.get("project", {}).items():
        if key in RESERVED_PROJECT_GROUPS:
            continue
        matches = {norm(value).lower() for value in config.get("match", []) if norm(value)}
        if any(project in matches for project in projects):
            return key
    return "ungrouped"


def project_group_key(project: str) -> str:
    """Return a stable config key for a newly observed canonical project value."""
    key = BLOCK_KEEP.sub("-", norm(project).lower()).strip("-._")
    return key or f"project-{sha(norm(project), 8)}"


def auto_project_groups(projects: Iterable[str], groups: dict[str, Any]) -> dict[str, Any]:
    """Groups for project values `groups.json` has never been told about.

    A project the config has not heard of used to fall into `ungrouped`, which
    made every new workstream look unclassified. Deriving the group instead
    keeps the answer a pure function of (pages, config) — same input, same
    key, same colour — while `register_project_groups` writes it down so the
    label and colour can then be edited by hand.
    """
    configured = groups.get("project", {})
    known = {norm(value).lower()
             for key, config in configured.items() if key not in RESERVED_PROJECT_GROUPS
             for value in config.get("match", []) if norm(value)}
    used_colors = {config.get("color") for config in configured.values()}
    additions: dict[str, Any] = {}
    for project in sorted({norm(value) for value in projects if norm(value)}):
        if project.lower() in known:
            continue
        known.add(project.lower())
        key = project_group_key(project)
        if key in configured or key in additions:
            key = f"{key}-{sha(project, 8)}"
        color = next((value for value in AUTO_PROJECT_COLORS if value not in used_colors),
                     AUTO_PROJECT_COLORS[len(additions) % len(AUTO_PROJECT_COLORS)])
        used_colors.add(color)
        label = project.upper() if re.fullmatch(r"[A-Za-z0-9._-]{1,12}", project) else project
        additions[key] = {"label": label, "color": color, "match": [project]}
    return additions


def merge_project_groups(groups: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
    """Ordinary groups, then the derived ones, with `multi`/`ungrouped` last."""
    if not additions:
        return groups
    configured = groups.get("project", {})
    ordinary = {k: v for k, v in configured.items() if k not in RESERVED_PROJECT_GROUPS}
    reserved = {k: v for k, v in configured.items() if k in RESERVED_PROJECT_GROUPS}
    return {**groups, "project": {**ordinary, **additions, **reserved}}


def register_project_groups(ws: Workspace, projects: Iterable[str], *, write: bool) -> list[str]:
    """Persist previously unseen project values so ingest cannot silently ungroup them."""
    groups = ws.load_groups()
    additions = auto_project_groups(projects, groups)
    if additions and write:
        dump(ws.groups_write_path, merge_project_groups(groups, additions), pretty=True)
    return list(additions)


def unresolved_count(page: dict[str, Any]) -> int:
    return sum(1 for b in page["blocks"].values()
               if b["kind"] == "conflict" and b.get("resolution", {}).get("status") != "resolved")


# 투영에 필요한 page 의 요약. 정본 파일에서도, 검색 색인(page/blk/link 표)에서도 같은 모양으로 만든다 —
# 그래서 증분 build 가 정본을 전부 다시 읽지 않고도 cold build 와 같은 파생물을 낸다.
def _record_from_page(rel: str, page: dict[str, Any]) -> dict[str, Any]:
    rec = {"id": page["id"], "slug": page["slug"], "title": page.get("title"), "type": page.get("type"),
           "created": page.get("created"), "updated": page.get("updated"),
           "projects": page.get("projects"), "tags": page.get("tags"), "sources": page.get("sources"),
           "source": rel, "sha256": sha(canonical(page)),
           "blocks": [(bid, page["blocks"][bid]["kind"]) for bid in page["block_order"]],
           "links": [], "unresolved": unresolved_count(page)}
    if "summary" in page:
        rec["summary"] = page["summary"]
    return rec


def _records_from_docs(docs: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    pages = [page for _, page in docs]
    lookup = page_lookup(pages)
    records = [_record_from_page(rel, page) for rel, page in docs]
    for rec, page in zip(records, pages):
        seen: set[str] = set()
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if not target or target["id"] == page["id"] or target["id"] in seen:
                continue
            # One line per ordered pair: a page that both links to and cites
            # another gets one edge, not two stacked on top of each other.
            seen.add(target["id"])
            rec["links"].append((target["id"], link["kind"]))
    return records


def _records_from_index(idx: "search_index.Index") -> list[dict[str, Any]]:
    """publish 된 색인의 page/blk/link 표에서 같은 요약을 읽는다 (정본 파일을 열지 않는다)."""
    db = idx.db
    records: dict[int, dict[str, Any]] = {}
    for rid, pid, slug, rel, sha256, meta_json, unresolved in db.execute(
            "SELECT rid, page_id, slug, source, sha256, meta, unresolved FROM page ORDER BY page_id"):
        meta = json.loads(meta_json or "{}")
        rec = {"id": pid, "slug": slug, "title": meta.get("title"), "type": meta.get("type"),
               "created": meta.get("created"), "updated": meta.get("updated"),
               "projects": meta.get("projects"), "tags": meta.get("tags"), "sources": meta.get("sources"),
               "source": rel, "sha256": sha256, "blocks": [], "links": [], "unresolved": int(unresolved or 0)}
        if "summary" in meta:
            rec["summary"] = meta["summary"]
        records[rid] = rec
    for prid, bid, kind in db.execute("SELECT prid, block_id, kind FROM blk WHERE pos >= 0 ORDER BY prid, pos"):
        records[prid]["blocks"].append((bid, kind))
    for src, dst_pid, kind in db.execute(
            "SELECT l.src, p.page_id, l.kind FROM link l JOIN page p ON p.rid=l.dst "
            "WHERE l.dst IS NOT NULL ORDER BY l.src, l.ord"):
        rec = records[src]
        if any(d == dst_pid for d, _k in rec["links"]):
            continue
        rec["links"].append((dst_pid, kind))
    return sorted(records.values(), key=lambda r: r["id"])


def _project_records(records: list[dict[str, Any]], groups: dict[str, Any]) -> dict[str, Any]:
    """Pure projection: page records -> derived artifacts (no disk writes)."""
    groups = merge_project_groups(
        groups, auto_project_groups((p for rec in records for p in rec["projects"]), groups))
    incoming: Counter[str] = Counter()
    outgoing: Counter[str] = Counter()
    edges: list[dict[str, Any]] = []
    for rec in records:
        for target_id, kind in rec["links"]:
            outgoing[rec["id"]] += 1
            incoming[target_id] += 1
            edges.append({"id": f"edge:{sha('|'.join((rec['id'], target_id)), 16)}", "source": rec["id"],
                          "target": target_id, "kind": kind})
    edges.sort(key=lambda e: (e["source"], e["target"], e["kind"]))

    buckets: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    nodes: list[dict[str, Any]] = []
    for rec in records:
        group = project_group(rec["projects"], groups)
        degree = incoming[rec["id"]] + outgoing[rec["id"]]
        node = {"id": rec["id"], "slug": rec["slug"], "label": rec["title"], "type": rec["type"],
                "created": rec["created"], "updated": rec["updated"],
                "projects": rec["projects"], "tags": rec["tags"], "group": group,
                "summary": rec.get("summary", ""), "incoming": incoming[rec["id"]],
                "outgoing": outgoing[rec["id"]], "degree": degree,
                "unresolved_conflicts": rec["unresolved"], "orphan": degree == 0,
                "data_url": f"pages/{safe_name(rec['id'])}"}
        buckets[group].append(node)
        nodes.append(node)
    # Allocate an angular sector proportional to each group's size. Within a
    # sector, a golden-ratio sequence distributes nodes from the centre to the
    # rim without rings or clumps. The resulting silhouette is a true disk,
    # while adjacent colours still make project groups easy to distinguish.
    configured_order = list(groups.get("project", {}))
    ordered_groups = [group for group in configured_order if buckets.get(group)]
    ordered_groups.extend(sorted(group for group in buckets if group not in configured_order))
    total_nodes = max(1, len(nodes))
    sector_start = -math.pi / 2
    for group in ordered_groups:
        group_nodes = sorted(buckets[group], key=lambda item: item["id"])
        sector_span = math.tau * len(group_nodes) / total_nodes
        angular_padding = min(0.035, sector_span * 0.04)
        usable_span = max(0.0, sector_span - angular_padding * 2)
        for i, node in enumerate(group_nodes):
            radial_fraction = math.sqrt((i + 0.72) / (len(group_nodes) + 0.72))
            angular_fraction = (i * GOLDEN_FRACTION) % 1.0
            angle = sector_start + angular_padding + usable_span * angular_fraction
            radius = LAYOUT_RADIUS * radial_fraction
            node["x"] = round(radius * math.cos(angle), 6)
            node["y"] = round(radius * math.sin(angle), 6)
        sector_start += sector_span
    nodes.sort(key=lambda n: n["id"])

    catalog = [{k: rec.get(k) for k in ("id", "slug", "title", "type", "updated",
                                        "projects", "tags", "sources", "summary")} for rec in records]
    map_pages: dict[str, Any] = {}
    map_blocks: dict[str, Any] = {}
    for rec in sorted(records, key=lambda r: r["id"]):
        # data_url serves the page as a standalone object; the empty RFC 6901
        # pointer selects that object root directly.
        map_pages[rec["id"]] = {"source": rec["source"], "pointer": "",
                                "data_url": f"pages/{safe_name(rec['id'])}", "sha256": rec["sha256"]}
        for bid, kind in rec["blocks"]:
            map_blocks[bid] = {"page_id": rec["id"], "pointer": f"/blocks/{bid}", "kind": kind,
                               "data_url": f"pages/{safe_name(rec['id'])}"}
    routes = {key: sorted(n["id"] for n in nodes if n["group"] == key) for key in groups["project"]}

    return {
        "catalog.json": catalog,
        "map.json": {"schema_version": ARTIFACT_VERSION, "pages": map_pages, "blocks": map_blocks},
        "graph.json": {"schema_version": ARTIFACT_VERSION, "nodes": nodes, "edges": edges, "groups": groups},
        "routes.json": routes,
        "stats.json": {"pages": len(records), "blocks": sum(len(r["blocks"]) for r in records),
                       "edges": len(edges),
                       "unresolved_conflicts": sum(n["unresolved_conflicts"] for n in nodes)},
    }


def check_documents(docs: list[tuple[str, dict[str, Any]]], validator: "SchemaValidator | None") -> None:
    """Schema + cross-page uniqueness; raises WikiError with every problem listed."""
    pages = [page for _, page in docs]
    errors = [e for page in pages for e in validate_page(page, validator)]
    if errors:
        raise WikiError("\n".join(errors))
    ids = [p["id"] for p in pages]
    slugs = [p["slug"] for p in pages]
    if len(ids) != len(set(ids)):
        raise WikiError(f"duplicate page id: {sorted(k for k, v in Counter(ids).items() if v > 1)}")
    if len(slugs) != len(set(slugs)):
        raise WikiError(f"duplicate page slug: {sorted(k for k, v in Counter(slugs).items() if v > 1)}")
    block_owner: dict[str, str] = {}
    for page in pages:
        for bid in page["block_order"]:
            if bid in block_owner:
                raise WikiError(f"duplicate block id {bid} in {block_owner[bid]} and {page['id']}")
            block_owner[bid] = page["id"]


def project(ws: Workspace) -> dict[str, Any]:
    """Pure projection: canonical pages -> derived artifacts (no disk writes)."""
    return _project_docs(ws, ws.load_documents())


def _project_docs(ws: Workspace, docs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    check_documents(docs, SchemaValidator.load(ws))
    return _project_records(_records_from_docs(docs), ws.load_groups())


def project_from_index(ws: Workspace) -> dict[str, Any]:
    """The same artifacts, read from the published search index instead of the canonical files."""
    idx = search_index.open_ro(ws.search_db)
    try:
        return _project_records(_records_from_index(idx), ws.load_groups())
    finally:
        idx.close()


# --------------------------------------------------------------------------- 델타 · 신선도
# 힌트 밖 파일의 변경 감지는 지난 build 가 `index/search.work.json` 에 파일마다 남긴 [mtime_ns, size] 와의
# 비교다 — 다르기만 하면(과거로 돌린 mtime 포함) sha 로 확인한다. 시각 문턱은 쓰지 않는다.
# 델타가 page 의 이 비율을 넘으면 cold build 가 더 싸다.
FULL_FRACTION = 0.25
MAP_PAGE_KEYS = ("source", "pointer", "data_url", "sha256")


def map_root(payload: dict[str, Any] | None) -> str:
    """`index/map.json` 의 page 부분 지문. revision 과 색인 meta.map_root 가 이 값으로 맞물린다."""
    if not payload:
        return ""
    pages = payload.get("pages") or {}
    return sha(canonical({pid: {k: entry.get(k) for k in MAP_PAGE_KEYS} for pid, entry in pages.items()}))


def revision_of(root: str, groups: dict[str, Any], *, heading_paths: bool) -> str:
    """산출물이 실제로 달라질 때만 바뀌는 값 — page 집합(map root)·그룹 설정·색인 옵션의 함수."""
    return sha(canonical({"map_root": root, "groups": groups, "heading_paths": bool(heading_paths),
                          "schema": search_index.SCHEMA_VERSION}))


def read_map(ws: Workspace) -> dict[str, Any] | None:
    path = ws.index / "map.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("pages"), dict) else None


def read_state(ws: Workspace) -> dict[str, Any] | None:
    try:
        value = json.loads(ws.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def wiki_files(ws: Workspace) -> dict[str, list[int]]:
    """wiki/**/*.json → [mtime_ns, size]. 수십 ms 짜리 스캔 — 정본을 열지 않는다."""
    out: dict[str, list[int]] = {}
    base = ws.pages_dir
    # ws.rel 은 symlink 를 풀어 저장소 상대 경로를 만든다. wiki/ 가 symlink 가 아니면 문자열로 자른다(10,000
    # 파일에서 0.3 초 → 30 ms); symlink 면 파일마다 ws.rel 로 간다.
    prefix = str(ws.root) + os.sep
    fast = os.path.realpath(str(base)) == str(base)
    stack = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.name.endswith(".json"):
                        try:
                            st = entry.stat()
                        except OSError:
                            continue
                        if fast and entry.path.startswith(prefix):
                            rel = entry.path[len(prefix):].replace(os.sep, "/")
                        else:
                            rel = ws.rel(Path(entry.path))
                        out[rel] = [st.st_mtime_ns, st.st_size]
        except OSError:
            continue
    return out


def load_file_pages(ws: Workspace, rel: str) -> list[dict[str, Any]]:
    """정본 파일 하나의 page 목록 (없으면 [])."""
    path = ws.root / rel
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WikiError(f"{rel}: invalid JSON ({exc})") from exc
    return [p for p in (value if isinstance(value, list) else [value]) if isinstance(p, dict)]


def file_shas_match(ws: Workspace, rel: str, old_pages: dict[str, Any]) -> bool:
    """파일의 page sha 들이 옛 map 의 항목과 정확히 같은가 (같은 source 의 항목만 본다)."""
    expected = {pid: e.get("sha256") for pid, e in old_pages.items() if e.get("source") == rel}
    try:
        pages = load_file_pages(ws, rel)
    except WikiError:
        return False
    found = {str(p.get("id")): sha(canonical(p)) for p in pages}
    return found == expected


def scan_changes(ws: Workspace, old_map: dict[str, Any], files: dict[str, Any], hint: set[str],
                 on_disk: dict[str, list[int]] | None = None) -> list[str]:
    """힌트 밖에서 바뀐 파일 목록. 지난 build 가 파일마다 기록한 `[mtime_ns, size]`(`files`, search.work.json) 와
    **다르기만 하면** — 더 오래된 mtime 이라도 — sha 로 확인하고, 같으면 파일을 열지 않는다.

    "지난 build 시각보다 새 mtime" 기준은 timestamp 를 보존한 복사나 utime 으로 과거로 돌린 편집을 놓쳤다
    (codex REVIEW #4). 기록이 없는 파일(새 파일)과 기록·map 에 있는데 없어진 파일은 그대로 변경으로 본다.
    기록이 없는 상태(옛 build) 에서는 알려진 파일을 전부 sha 로 확인한다 — 느리지만 안전하다."""
    on_disk = wiki_files(ws) if on_disk is None else on_disk
    old_pages = old_map.get("pages") or {}
    known = {str(e.get("source")) for e in old_pages.values()}
    outside: list[str] = []
    for rel in sorted((known | set(files)) - set(on_disk)):
        if rel not in hint:
            outside.append(rel)
    for rel, stat in sorted(on_disk.items()):
        if rel in hint:
            continue
        if rel not in known and rel not in files:
            outside.append(rel)
        elif list(files.get(rel) or ()) != list(stat) and not file_shas_match(ws, rel, old_pages):
            outside.append(rel)
    return outside


def page_hash_map(ws: Workspace, changed: Iterable[str] | None = None, *, old_map: dict[str, Any] | None = None
                  ) -> tuple[dict[str, Any], dict[str, tuple[str, dict[str, Any]]]]:
    """(map.json 의 pages, 다시 읽은 문서 {page_id: (source, page)}).

    `changed` 가 None 이면 전량(정본을 모두 읽고 검증), 아니면 그 파일만 다시 읽어 sha 를 내고 나머지는
    `old_map` 의 항목을 재사용한다. 힌트 밖의 변경은 `scan_changes` 가 따로 잡는다 — 여기서는 믿는다.
    """
    validator = SchemaValidator.load(ws)
    if changed is None:
        docs = ws.load_documents()
        check_documents(docs, validator)
        payload = _project_records(_records_from_docs(docs), {"project": {}})["map.json"]
        return payload["pages"], {str(p["id"]): (rel, p) for rel, p in docs}
    hint = {ws.rel(ws.root / rel) if not os.path.isabs(rel) else ws.rel(Path(rel)) for rel in changed}
    old_pages = dict((old_map or {}).get("pages") or {})
    pages = {pid: e for pid, e in old_pages.items() if str(e.get("source")) not in hint}
    docs: dict[str, tuple[str, dict[str, Any]]] = {}
    for rel in sorted(hint):
        for page in load_file_pages(ws, rel):
            errors = validate_page(page, validator)
            if errors:
                raise WikiError("\n".join(errors))
            pid = str(page["id"])
            if pid in docs:
                raise WikiError(f"duplicate page id: {pid} ({docs[pid][0]}, {rel})")
            if pid in pages and (ws.root / str(pages[pid].get("source"))).is_file():
                raise WikiError(f"duplicate page id: {pid} ({pages[pid].get('source')}, {rel})")
            docs[pid] = (rel, page)
            pages[pid] = {"source": rel, "pointer": "", "data_url": f"pages/{safe_name(pid)}",
                          "sha256": sha(canonical(page))}
    return dict(sorted(pages.items())), docs


map_delta = search_index.map_delta


def build(ws: Workspace, *, changed: Iterable[str] | None = None, full: bool = False,
          heading_paths: bool = False) -> dict[str, Any]:
    """정본 → index/*.json + search.sqlite + viewer 데이터. 결과는 cold 든 증분이든 같은 바이트다.
    자리는 배치가 정한다: 단일 repo 는 `index/`·`viewer/public/data/`, 분리 모드는 `.llmwiki/index/`·
    `.llmwiki/viewer-data/` (git 에서 빠진다). 산출물에 적힌 경로는 어느 쪽이든 데이터 root 상대경로다.

    `changed` 는 힌트다: 그 파일만 다시 sha 를 내고, `wiki/**/*.json` mtime 스캔으로 힌트 밖의 변경이
    보이면 전량으로 떨어진다. 증분은 검색 색인의 바뀐 page 행만 갈아 끼우고(`llmwiki_index.update`),
    파생물은 publish 된 색인의 표에서 투영한다. `index/map.json`·`revision.json` 은 publish 가 성공한
    뒤에만 쓴다.
    """
    started_ns = time.time_ns()
    phases: dict[str, float] = {}
    clock = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal clock
        now = time.perf_counter()
        phases[name] = round(phases.get(name, 0.0) + (now - clock) * 1000, 1)
        clock = now

    groups = ws.load_groups()
    mode, reason = "full", ""
    old_map = read_map(ws)
    state = read_state(ws)
    old_root = map_root(old_map)
    on_disk = wiki_files(ws)          # 정본을 읽기 전의 스냅샷 — 그 뒤 바뀐 파일은 다음 build 가 다른 (mtime,size) 로 잡는다
    hint: set[str] | None = None
    if changed is not None and not full:
        hint = {ws.rel(ws.root / rel) if not os.path.isabs(rel) else ws.rel(Path(rel)) for rel in changed}
        if not old_map or not state:
            reason = "no-previous-build"
        elif str(state.get("schema")) != search_index.SCHEMA_VERSION or bool(state.get("heading_paths")) != bool(heading_paths):
            reason = "index-options-changed"
        elif str(state.get("map_root")) != old_root:
            reason = "map-root-mismatch"
        else:
            outside = scan_changes(ws, old_map, dict(state.get("files") or {}), hint, on_disk)
            if outside:
                reason = "unhinted-change:" + ",".join(outside[:3])
            else:
                mode = "incremental"
    elif full:
        reason = "full-requested"
    else:
        reason = "no-hint"
    lap("scan")

    built: dict[str, Any] = {}
    delta = search_index.Delta()
    docs_by_id: dict[str, tuple[str, dict[str, Any]]] = {}
    if mode == "incremental":
        assert old_map is not None and hint is not None
        new_pages, docs_by_id = page_hash_map(ws, hint, old_map=old_map)
        delta = map_delta(old_map["pages"], new_pages)
        new_root = map_root({"pages": new_pages})
        lap("hash")
        if len(delta) > FULL_FRACTION * max(len(new_pages), 1):
            mode, reason = "full", "large-delta"
        elif not delta:
            reason = "no-change"
        else:
            revision = revision_of(new_root, groups, heading_paths=heading_paths)

            def loader(page_id: str, rel: str) -> dict[str, Any] | None:
                return next((p for p in load_file_pages(ws, rel) if str(p.get("id")) == page_id), None)

            try:
                built = search_index.update(ws.search_db, docs_by_id, delta, revision=revision, map_root=new_root,
                                            expect_map_root=old_root, loader=loader, heading_paths=heading_paths)
            except search_index.IndexError_ as exc:
                mode, reason = "full", f"index:{exc}"
            lap("index")
    if mode == "incremental":
        if delta:
            payloads = project_from_index(ws)
            lap("project")
            if map_root(payloads["map.json"]) != new_root:      # 색인과 map 이 어긋나면 믿지 않는다
                mode, reason = "full", "projection-mismatch"
        else:
            payloads = None
    if mode == "full":
        docs = ws.load_documents()
        payloads = _project_docs(ws, docs)
        new_root = map_root(payloads["map.json"])
        revision = revision_of(new_root, groups, heading_paths=heading_paths)
        built = search_index.build(docs, ws.search_db, revision=revision, heading_paths=heading_paths,
                                   map_root=new_root)
        docs_by_id = {str(p["id"]): (rel, p) for rel, p in docs}
        delta = search_index.Delta(added=sorted(docs_by_id))
        lap("full")

    stats: dict[str, Any]
    if payloads is None:                                    # 증분인데 델타가 비었다: 산출물은 그대로다
        stats = dict(json.loads((ws.index / "stats.json").read_text(encoding="utf-8")))
        # 파일 기록은 갱신한다 — touch 만 된 파일을 sha 로 확인했으니 다음 build 는 열지 않아도 된다
        dump(ws.state_path, {"schema": search_index.SCHEMA_VERSION, "started_ns": started_ns,
                             "map_root": new_root, "heading_paths": bool(heading_paths),
                             "files": dict(sorted(on_disk.items()))})
    else:
        payloads["revision.json"] = {"schema_version": ARTIFACT_VERSION, "revision": revision,
                                     "search_root": built["digest"]}
        for name, data in payloads.items():
            dump(ws.index / name, data, pretty=True)
            dump(ws.public / name, data)
        shard_dir = ws.public / "pages"
        if mode == "full":
            shutil.rmtree(shard_dir, ignore_errors=True)
            shard_dir.mkdir(parents=True, exist_ok=True)
            for _pid, (_rel, page) in docs_by_id.items():
                dump(shard_dir / safe_name(page["id"]), page)
        else:
            for pid in delta.deleted:
                (shard_dir / safe_name(pid)).unlink(missing_ok=True)
            for pid in [*delta.added, *delta.modified]:
                dump(shard_dir / safe_name(pid), docs_by_id[pid][1])
        stats = dict(payloads["stats.json"])
        dump(ws.state_path, {"schema": search_index.SCHEMA_VERSION, "started_ns": started_ns,
                             "map_root": new_root, "heading_paths": bool(heading_paths),
                             "files": dict(sorted(on_disk.items()))})
        lap("write")
    stats["mode"] = mode
    stats["phases"] = phases
    stats["reason"] = reason
    stats["delta"] = {"added": len(delta.added), "modified": len(delta.modified), "deleted": len(delta.deleted)}
    if built:
        stats["index"] = {k: built[k] for k in ("bytes", "digest", "mode") if k in built}
        if "compacted" in built:
            stats["index"]["compacted"] = built["compacted"]
        if "reindexed" in built:
            stats["index"]["reindexed"] = built["reindexed"]
    stats["ms"] = round((time.time_ns() - started_ns) / 1e6, 1)
    return stats


# --------------------------------------------------------------------------- render
def flow_list(items: Iterable[str]) -> str:
    """`a, "b, c"` — what `split_flow` reads back as the same items."""
    def quoted(item: str) -> str:
        if not re.search(r"[,\[\]\"']", item):
            return item
        return f"'{item}'" if '"' in item else f'"{item}"'
    return ", ".join(quoted(item) for item in items)


def frontmatter(page: dict[str, Any]) -> list[str]:
    """Frontmatter `ingest` reads back as the same page head — so `render` → edit → `ingest --update`
    is how tags, projects, sources, supersedes, related, raw and summary change."""
    rows = ["---", f"type: {page['type']}", f"created: {page['created']}",
            f"updated: {page['updated']}", f"tags: [{flow_list(page['tags'])}]",
            f"projects: [{flow_list(page['projects'])}]",
            f"sources: [{flow_list(page['sources'])}]"]
    if page.get("keywords"):
        rows.append(f"keywords: [{flow_list(page['keywords'])}]")
    for kind in ("supersedes", "related"):
        targets = list(dict.fromkeys(str(link.get("target")) for link in page.get("links") or []
                                     if link.get("kind") == kind))
        if targets:
            rows.append(f"{kind}: [{flow_list(targets)}]")
    if page.get("raw_ref"):
        rows.append(f"raw: {page['raw_ref']}")
    if page.get("summary"):
        rows.append('summary: "' + " ".join(str(page["summary"]).split()) + '"')
    return rows + ["---", ""]


def render_markdown(page: dict[str, Any], exact: bool = False) -> str:
    """`exact` replays the ingested snapshot byte-for-byte; otherwise blocks are joined."""
    snapshot = page.get("source_snapshot")
    if exact:
        if not snapshot or snapshot.get("format") != "markdown":
            raise WikiError(f"{page['id']}: no markdown snapshot for --exact")
        return snapshot["text"]
    body = "\n\n".join(page["blocks"][bid]["source_text"] for bid in page["block_order"])
    return "\n".join(frontmatter(page)) + body.rstrip() + "\n"


def wiki_to_html(text: str) -> str:
    escaped = html.escape(str(text or ""))
    return WIKILINK.sub(
        lambda m: '<a href="#" data-wiki-target="{}">{}</a>'.format(
            html.escape(norm(m.group(1))), html.escape(norm(m.group(3)) or norm(m.group(1)))),
        escaped)


def render_block_html(block: dict[str, Any]) -> str:
    data = block.get("data", {})
    kind = block["kind"]
    if kind == "heading":
        level = min(6, max(1, int(data.get("level", 2))))
        return f"<h{level}>{wiki_to_html(data.get('text', ''))}</h{level}>"
    if kind in {"paragraph", "markdown", "raw"}:
        return "<p>{}</p>".format(
            wiki_to_html(data.get("text", block["source_text"])).replace("\n", "<br>"))
    if kind == "code":
        return '<pre><code data-language="{}">{}</code></pre>'.format(
            html.escape(str(data.get("language", ""))), html.escape(str(data.get("text", ""))))
    if kind == "table":
        rows = data.get("rows", [])
        out = ["<table>"]
        for idx, row in enumerate(rows):
            if idx == 1 and row and all(re.fullmatch(r":?-+:?", c) for c in row):
                continue
            tag = "th" if idx == 0 else "td"
            out.append("<tr>" + "".join(f"<{tag}>{wiki_to_html(c)}</{tag}>" for c in row) + "</tr>")
        out.append("</table>")
        return "".join(out)
    if kind == "list":
        tag = "ol" if data.get("ordered") else "ul"
        items = data.get("items") or [
            line.strip() for line in str(data.get("text", "")).splitlines() if line.strip()]
        return f"<{tag}>" + "".join(f"<li>{wiki_to_html(i)}</li>" for i in items) + f"</{tag}>"
    if kind in {"quote", "conflict", "current", "open_question"}:
        return f'<blockquote class="{kind}">{wiki_to_html(data.get("text", ""))}</blockquote>'
    if kind == "thematic_break":
        return "<hr>"
    return f"<pre>{html.escape(block['source_text'])}</pre>"


def render_html(page: dict[str, Any]) -> str:
    body = "".join(render_block_html(page["blocks"][bid]) for bid in page["block_order"])
    return f'<article data-page-id="{html.escape(page["id"])}">{body}</article>'


# --------------------------------------------------------------------------- addressing
def split_address(selector: str) -> tuple[str, str | None]:
    selector = norm(selector)
    if selector.startswith("block:"):
        return "", selector
    if "#" in selector:
        page_sel, block_sel = selector.split("#", 1)
        return page_sel.strip(), block_sel.strip()
    return selector, None


def find_document(ws: Workspace, selector: str) -> tuple[str, dict[str, Any]]:
    """(canonical shard, page) — the shard is where a write to this page has to go."""
    selector = norm(selector)
    docs = ws.load_documents()
    for rel, page in docs:
        if selector in {page["id"], page["slug"], page["id"].removeprefix("page:")}:
            return rel, page
    # Same tolerance the graph uses: a title or a differently-cased slug still lands.
    match = page_lookup(page for _, page in docs).get(link_key(selector))
    if match:
        return next(rel for rel, page in docs if page is match), match
    raise WikiError(f"page not found: {selector}")


def find_page(ws: Workspace, selector: str) -> dict[str, Any]:
    return find_document(ws, selector)[1]


def live_block_id(page: dict[str, Any], block_id: str) -> str | None:
    """The block's current id: itself, or the id an `edit` gave it (page `block_aliases`)."""
    blocks = page.get("blocks") or {}
    if block_id in blocks:
        return block_id
    target = (page.get("block_aliases") or {}).get(block_id)
    return target if target in blocks else None


def find_block(ws: Workspace, block_id: str, page: dict[str, Any] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = [page] if page else ws.load_pages()
    for candidate in candidates:
        block = candidate["blocks"].get(block_id)
        if block:
            return candidate, block
    # An id that `edit` replaced keeps resolving to the block that took its place.
    for candidate in candidates:
        live = live_block_id(candidate, block_id)
        if live:
            return candidate, candidate["blocks"][live]
    # Allow addressing a block by its bare fingerprint too.
    for candidate in candidates:
        for bid, block in candidate["blocks"].items():
            if bid.endswith(f":{block_id}") or block.get("fingerprint") == block_id:
                return candidate, block
    raise WikiError(f"block not found: {block_id}")


def resolve(ws: Workspace, selector: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    page_sel, block_sel = split_address(selector)
    page = find_page(ws, page_sel) if page_sel else None
    if not block_sel:
        if page is None:
            raise WikiError(f"page not found: {selector}")
        return page, None
    page, block = find_block(ws, block_sel, page)
    return page, follow_current(page, block)


def json_pointer(value: Any, pointer: str) -> Any:
    """RFC 6901 pointer; '' selects the whole document."""
    if not pointer or pointer == "/":
        return value
    for token in pointer.lstrip("/").split("/"):
        key = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"\d+", key) or int(key) >= len(value):
                raise WikiError(f"pointer {pointer}: bad array index {key!r}")
            value = value[int(key)]
        elif isinstance(value, dict):
            if key not in value:
                raise WikiError(f"pointer {pointer}: missing key {key!r}")
            value = value[key]
        else:
            raise WikiError(f"pointer {pointer}: cannot descend into {type(value).__name__}")
    return value


def pick_fields(value: Any, fields: Iterable[str]) -> Any:
    for field in fields:
        if isinstance(value, list) and re.fullmatch(r"\d+", field):
            value = value[int(field)]
        elif isinstance(value, dict) and field in value:
            value = value[field]
        else:
            raise WikiError(f"field not found: {field}")
    return value


def outline(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Cheap section index — read a section instead of the whole page."""
    rows = []
    for position, bid in enumerate(page["block_order"]):
        block = page["blocks"][bid]
        if block["kind"] == "heading":
            rows.append({"block_id": bid, "level": block["data"].get("level", 2),
                         "text": block["data"].get("text", ""), "position": position})
    return rows


def section(page: dict[str, Any], block_id: str) -> list[dict[str, Any]]:
    """The heading block plus every block up to the next heading of the same/higher level."""
    order = page["block_order"]
    block_id = live_block_id(page, block_id) or block_id
    if block_id not in order:
        raise WikiError(f"{page['id']}: no block {block_id}")
    start = order.index(block_id)
    head = page["blocks"][block_id]
    if head["kind"] != "heading":
        return [head]
    level = head["data"].get("level", 2)
    picked = [head]
    for bid in order[start + 1:]:
        block = page["blocks"][bid]
        if block["kind"] == "heading" and block["data"].get("level", 2) <= level:
            break
        picked.append(block)
    return [follow_current(page, block) for block in picked]


# --------------------------------------------------------------------------- lint
def lint(ws: Workspace, docs: list[tuple[str, dict[str, Any]]] | None = None) -> tuple[list[str], list[str]]:
    docs = ws.load_documents() if docs is None else docs
    pages = [p for _, p in docs]
    validator = SchemaValidator.load(ws)
    by_slug: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    warnings: list[str] = []

    for page in pages:
        errors.extend(validate_page(page, validator))
        errors.extend(consistency_errors(page))
        slug = page.get("slug")
        if slug in by_slug:
            errors.append(f"{page.get('id')}: duplicate slug {slug}")
        by_slug[slug] = page

    seen_blocks: dict[str, str] = {}
    for page in pages:
        for bid in page.get("block_order", []):
            if bid in seen_blocks:
                errors.append(f"{page['id']}: duplicate block id {bid} (also in {seen_blocks[bid]})")
            seen_blocks[bid] = page["id"]

    lookup = page_lookup(pages)
    for page in pages:
        for link in implied_links(page):
            # A `source:` ref may name evidence that has no page of its own;
            # a [[wikilink]] that resolves to nothing is always a mistake.
            if link["kind"] == "wiki" and lookup.get(link_key(link["target"])) is None:
                errors.append(f"{page['id']}: missing link target [[{link['target']}]]")
        for ref in page.get("sources", []):
            if str(ref).startswith("page:") and link_key(str(ref)) not in lookup:
                errors.append(f"{page['id']}: source ref {ref} has no page")
        for bid in page.get("block_order", []):
            block = page["blocks"][bid]
            if block["kind"] == "conflict" and block.get("resolution", {}).get("status") != "resolved":
                warnings.append(f"{page['id']}:{bid}: unresolved conflict")
            elif block["kind"] == "open_question":
                data = block.get("data") if isinstance(block.get("data"), dict) else {}
                due = data.get("due")
                late = " — 기한 지남" if due and str(due) < today() else ""
                warnings.append(f"{page['id']}:{bid}: open question (owner {data.get('owner') or '없음'}, "
                                f"due {due or '없음'}){late}")
        if not page.get("summary"):
            warnings.append(f"{page['id']}: empty summary")

    degree: Counter[str] = Counter()
    for page in pages:
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if target is not None and target["id"] != page["id"]:
                degree[page["slug"]] += 1
                degree[target["slug"]] += 1
    for page in pages:
        if page.get("type") not in UNLINKED_TYPES and degree[page.get("slug")] == 0:
            warnings.append(f"{page['id']}: orphan (no inbound or outbound links)")

    warnings.extend(compile_warnings(pages, lookup))
    warnings.extend(tag_warnings(pages, ws.load_tag_vocabulary(), ws.rel(ws.tags_path)))
    warnings.extend(stale_index(ws))
    return sorted(dict.fromkeys(errors)), sorted(dict.fromkeys(warnings))


# --------------------------------------------------------------------------- compile quality
# 소스를 한 번 통합해 두는 위키라면 보이는 냄새들. 전부 경고다 — 고칠지는 사람이 정한다.
COMPILED_TYPES = frozenset({"concept", "entity", "synthesis"})
DEBT_TYPES = frozenset({"concept"})          # 이 type 의 updated 보다 새 source 가 compile debt 다
PAGE_MAX_BYTES = 200 * 1024
SUMMARY_HEADER_DUMP = re.compile(r"^(?:항목|구분)\s*\|?\s*(?:값|내용)(?:\s|\||$)")
TABLE_RULE_DUMP = re.compile(r"(?:^|\s)\|?\s*:?-{3,}:?\s*\|?\s+\|?\s*:?-{3,}:?")
SUPERSEDE_PHRASE = re.compile(
    r"(?:을|를)\s*(?:대체|갈음)(?:한다|합니다|했다|하였다|함|하며|하고|하는)|(?i:\b(?:supersedes|replaces|obsoletes)\b)")


def summary_is_dump(page: dict[str, Any]) -> bool:
    """요약이 표 덤프다: `항목 값` 머리로 시작하거나, 표 행·구분선이거나, 표 block 을 이어 붙인 글이다."""
    summary = norm(page.get("summary"))
    if not summary:
        return False
    if SUMMARY_HEADER_DUMP.match(summary) or summary.startswith("|") or TABLE_RULE_DUMP.search(summary[:120]):
        return True
    blocks = page.get("blocks") or {}
    for bid in page.get("block_order") or []:
        block = blocks.get(bid) or {}
        if block.get("kind") != "table":
            continue
        flat = summary_from_blocks({bid: block}, [bid])
        if len(flat) >= 8 and summary.startswith(flat[:40]):
            return True
    return False


def compile_warnings(pages: list[dict[str, Any]], lookup: dict[str, dict[str, Any]]) -> list[str]:
    """요약 덤프, 개념에 닿지 않은 source, compile debt, 너무 큰 page, supersedes 없는 대체 문장."""
    warnings: list[str] = []
    neighbours: defaultdict[str, set[str]] = defaultdict(set)    # page id → source/page 로 이어진 page id
    outgoing: defaultdict[str, set[str]] = defaultdict(set)
    for page in pages:
        for link in implied_links(page):
            target = lookup.get(link_key(link["target"]))
            if target is not None and target["id"] != page["id"]:
                outgoing[page["id"]].add(target["id"])
                neighbours[page["id"]].add(target["id"])
                neighbours[target["id"]].add(page["id"])
    by_id = {page["id"]: page for page in pages}
    for page in pages:
        pid = page["id"]
        if summary_is_dump(page):
            warnings.append(f"{pid}: summary starts with a table dump — 한 문장 요약으로 바꾼다")
        if page.get("type") == "source" and not any(
                by_id[target].get("type") in COMPILED_TYPES for target in outgoing[pid]):
            warnings.append(f"{pid}: source links to no concept, entity or synthesis — 아직 통합되지 않았다")
        if page.get("type") in DEBT_TYPES:
            newer = sorted(by_id[other]["slug"] for other in neighbours[pid]
                           if by_id[other].get("type") == "source"
                           and str(by_id[other].get("updated") or "") > str(page.get("updated") or ""))
            if newer:
                warnings.append(f"{pid}: compile debt {len(newer)} — 이 concept 보다 새 source: "
                                + ", ".join(newer[:5]) + (" …" if len(newer) > 5 else ""))
        size = len(json.dumps(page, ensure_ascii=False, indent=2).encode("utf-8"))
        if size > PAGE_MAX_BYTES:
            warnings.append(f"{pid}: page is {size // 1024} KB (> {PAGE_MAX_BYTES // 1024} KB) — 나눈다")
        if not any(link["kind"] == "supersedes" for link in implied_links(page)):
            blocks = page.get("blocks") or {}
            for bid in page.get("block_order") or []:
                block = blocks.get(bid) or {}
                found = SUPERSEDE_PHRASE.search(str(block.get("source_text") or "")) \
                    if block.get("kind") != "code" else None
                if found:
                    warnings.append(f"{pid}:{bid}: 본문이 '{found.group(0)}' 라고 하지만 supersedes 링크가 없다")
                    break
    return warnings


def sorted_tags(page: dict[str, Any], spellings: dict[str, str],
                allowed: set[str] | None) -> tuple[list[str], list[str]]:
    """page 의 (tags, keywords). 표기 변형은 한 표기로 모으고, 허용 목록이 있으면 목록 안의 낱말만 tag 로
    둔다 — 목록 밖의 tag 는 keywords 로 내리고 목록에 든 keyword 는 tag 로 올린다. 목록을 고치고 다시
    돌리면 그대로 되돌아간다. 목록 안의 낱말이 하나도 없는 page 는 tags 를 비울 수 없어 그대로 둔다."""
    tags = canonical_tags(page.get("tags"), spellings)
    keywords = string_list(page.get("keywords"))
    if allowed is None:
        return tags, keywords
    listed = canonical_tags([word for word in tags + keywords if tag_key(word) in allowed], spellings)
    if not listed:
        return tags, keywords
    rest: list[str] = []
    seen = {tag_key(word) for word in listed}
    for word in keywords + tags:                       # 있던 keywords 가 앞, 내린 tag 가 뒤
        key = tag_key(word)
        if key and key not in seen:
            seen.add(key)
            rest.append(word)
    return listed, rest


def tag_warnings(pages: list[dict[str, Any]], vocabulary: list[str] | None,
                 source: str = "tools/config/tags.json") -> list[str]:
    """한 tag 의 여러 표기, 허용 목록 밖의 tag. 목록이 없으면 표기만 본다."""
    spellings = tag_spellings(pages, vocabulary)
    allowed = {tag_key(tag) for tag in vocabulary} if vocabulary is not None else None
    warnings: list[str] = []
    for page in pages:
        for tag in string_list(page.get("tags")):
            key = tag_key(tag)
            if spellings.get(key, tag) != tag:
                warnings.append(f"{page['id']}: tag '{tag}' 는 '{spellings[key]}' 의 변형이다 — repair 가 한 표기로 모은다")
            elif allowed is not None and key not in allowed:
                warnings.append(f"{page['id']}: tag '{tag}' 가 {source} 에 없다 — "
                                "목록에 더하거나 repair 로 keywords 에 옮긴다")
    return warnings


# lint 경고의 종류 — `lint --json` 의 warning_kinds 가 종류별로 센다. 처음 맞는 것이 그 종류다.
WARNING_KINDS = (
    ("unresolved_conflict", re.compile(r": unresolved conflict$")),
    ("open_question", re.compile(r": open question \(")),
    ("empty_summary", re.compile(r": empty summary$")),
    ("orphan", re.compile(r": orphan \(")),
    ("summary_dump", re.compile(r": summary starts with a table dump")),
    ("unintegrated_source", re.compile(r": source links to no concept, entity or synthesis")),
    ("compile_debt", re.compile(r": compile debt \d+")),
    ("page_too_large", re.compile(r": page is \d+ KB")),
    ("missing_supersedes", re.compile(r"라고 하지만 supersedes 링크가 없다$")),
    ("tag_variant", re.compile(r": tag '.*' 는 '.*' 의 변형이다")),
    ("tag_off_list", re.compile(r": tag '.*' 가 .* 에 없다")),
    ("stale_index", re.compile(r"map\.json (?:missing|unreadable|stale)")),
)


def warning_kinds(warnings: Iterable[str]) -> dict[str, int]:
    """경고 종류별 개수 (0 인 종류는 싣지 않는다, 어느 종류에도 맞지 않으면 other)."""
    counts: Counter[str] = Counter()
    for warning in warnings:
        counts[next((kind for kind, pattern in WARNING_KINDS if pattern.search(warning)), "other")] += 1
    order = [kind for kind, _ in WARNING_KINDS] + ["other"]
    return {kind: counts[kind] for kind in order if counts[kind]}


def issue_counts(pages: Iterable[dict[str, Any]]) -> dict[str, int]:
    """lint 가 따로 세는 열린 일 — 미판정 상충과 미결 질문은 다른 일이다."""
    counts = {"unresolved_conflicts": 0, "open_questions": 0}
    for page in pages:
        for block in (page.get("blocks") or {}).values():
            if isinstance(block, dict) and T.unresolved(block):
                counts["unresolved_conflicts"] += 1
            elif isinstance(block, dict) and block.get("kind") == "open_question":
                counts["open_questions"] += 1
    return counts


def stale_index(ws: Workspace) -> list[str]:
    """index/map.json 이 정본과 맞는가 — 전량 재투영 대신 mtime 스캔 + 후보 파일의 sha 대조. 아무것도 쓰지 않는다."""
    path = ws.index / "map.json"
    if not path.exists():
        return ["index/map.json missing — run build"]
    current = read_map(ws)
    if current is None:
        return ["index/map.json unreadable — run build"]
    state = read_state(ws)
    if scan_changes(ws, current, dict((state or {}).get("files") or {}), set()):
        return ["index/map.json stale — run build"]
    return []


# --------------------------------------------------------------------------- log
def append_log(ws: Workspace, entry: dict[str, Any]) -> dict[str, Any]:
    record = {"at": timestamp(), **entry}
    ws.log_path.parent.mkdir(parents=True, exist_ok=True)
    with ws.log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_log(ws: Workspace, limit: int = 10) -> list[dict[str, Any]]:
    if not ws.log_path.exists():
        return []
    rows = [json.loads(line) for line in ws.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows[-limit:] if limit > 0 else rows


# --------------------------------------------------------------------------- ingest
def ingest(ws: Workspace, source: Path, page_type: str | None = None,
           projects: list[str] | None = None, summary: str = "",
           update: bool = False, dry_run: bool = False, slug: str = "",
           tags: list[str] | None = None) -> dict[str, Any]:
    """Write one canonical page. raw/ is read-only and is verified untouched."""
    path = Path(source).resolve()
    if not path.exists() or not path.is_file():
        raise WikiError(f"source not found: {source}")
    try:
        path.relative_to(ws.knowledge)
        raise WikiError("wiki/ is the canonical store; ingest reads from raw/, not from it")
    except ValueError:
        pass

    before = sha_bytes(path.read_bytes())
    suffix = path.suffix.lower()
    converted, fmt = "", "markdown"
    if suffix in {".md", ".markdown", ""}:
        text = path.read_text(encoding="utf-8")
    elif suffix == ".json" and is_page_object(path):
        text = path.read_text(encoding="utf-8")
    else:
        # pdf / office / html / csv / code / binary — extract what is readable and
        # fall back to a metadata-only stub so nothing in raw/ is skipped.
        extractors = load_extractors()
        body, has_text = extractors.extract(path)
        converted = body or stub_body(ws, path)
        converted, redacted = redact_secrets(converted)
        if redacted:
            print(f"note: {ws.rel(path)} — 보안정보 {redacted}건 가림", file=sys.stderr)
        fmt = "markdown" if has_text else "text"
        text = converted
    assert_no_secrets(text, ws.rel(path))

    if suffix == ".json" and not converted:
        page = json.loads(text)
        if isinstance(page, list):
            raise WikiError("ingest takes a single page object, not an array")
        page.setdefault("raw_ref", repo_path(ws, path))
        if page_type:
            page["type"] = page_type
        if projects:
            page["projects"] = projects
        if summary:
            page["summary"] = summary
        if slug:
            page["slug"] = norm(slug).replace(" ", "-").lower()
            page["id"] = f"page:{page['slug']}"
        for key in ("tags", "keywords", "projects", "sources"):
            if key in page:
                page[key] = string_list(page[key])
        if "tags" in page:
            page["tags"] = canonical_tags(page["tags"], {tag_key(tag): tag for tag in ws.load_tag_vocabulary() or ()})
        page.setdefault("history", []).append(
            {"at": today(), "action": "ingested", "actor": "llmwiki-cli",
             "note": repo_path(ws, path) or path.name})
    else:
        page = page_from_markdown(ws, path, page_type, projects, summary, slug,
                                  converted, fmt, tags)

    # 쓰기 경로는 어긋난 page 를 들이지 않는다 — markdown 은 파서가 만들어 늘 맞고, JSON 소스는 여기서 걸린다.
    errors = validate_page(page, SchemaValidator.load(ws)) + consistency_errors(page)
    if errors:
        raise WikiError("\n".join(errors))

    dest = ws.knowledge / PAGE_DIRS[page["type"]] / safe_name(page["id"])
    existing = next((p for p in ws.knowledge.rglob("*.json")
                     if p.name == dest.name and p.is_file()), None) if ws.knowledge.exists() else None
    if existing and not update:
        raise WikiError(f"{page['id']} already exists at {ws.rel(existing)} — pass --update to replace")
    if existing:
        prior = json.loads(existing.read_text(encoding="utf-8"))
        if not isinstance(prior, dict):
            raise WikiError(f"{ws.rel(existing)} holds several pages in one file — "
                            "change its blocks with `edit` instead of ingesting over it")
        if norm(prior.get("id")) != page["id"]:
            raise WikiError(f"{ws.rel(existing)} already holds {prior.get('id')} — "
                            f"{page['id']} would overwrite a different page")
        page["created"] = prior.get("created", page["created"])
        page["history"] = [*prior.get("history", []), *page["history"]]
        carry_over(prior, page)

    registered_groups = register_project_groups(ws, page["projects"], write=not dry_run)
    # A page whose type changed belongs in the directory of its new type —
    # leaving it behind would file an entity under wiki/sources/.
    moved_from = existing if existing and existing.resolve() != dest.resolve() else None
    result = {"page_id": page["id"], "dest": ws.rel(dest), "blocks": len(page["blocks"]),
              "updated": bool(existing), "dry_run": dry_run,
              "registered_groups": registered_groups}
    if moved_from:
        result["moved_from"] = ws.rel(moved_from)
    if dry_run:
        return result

    dump(dest, page, pretty=True)
    if moved_from:
        moved_from.unlink(missing_ok=True)
    if sha_bytes(path.read_bytes()) != before:
        raise WikiError(f"source {ws.rel(path)} changed during ingest — raw/ must stay immutable")
    append_log(ws, {"action": "ingest", "page_id": page["id"],
                    "source": repo_path(ws, path) or path.name, "dest": ws.rel(dest),
                    "mode": "update" if existing else "create",
                    "registered_groups": registered_groups})
    # 파생물은 build 로만 갱신한다 — 바뀐 파일만 힌트로 넘겨 색인의 그 page 행만 갈아 끼운다.
    changed = [ws.rel(dest)] + ([ws.rel(moved_from)] if moved_from else [])
    result["build"] = build(ws, changed=changed)
    return result


def keep_block_ids(prior: dict[str, Any], page: dict[str, Any]) -> None:
    """Blocks whose content the update did not change keep their old ids.

    A re-parse numbers repeated content by position, so after an edit to one of
    two equal paragraphs `…:2` would come back as `…:1`, the old id would vanish
    and an alias would point somewhere else. Equal content takes the prior id of
    the same fingerprint first; the rest keep theirs or get one no prior id or
    alias holds.
    """
    pool: dict[str, list[str]] = {}
    for bid in prior.get("block_order") or []:
        block = (prior.get("blocks") or {}).get(bid)
        fingerprint = str(block.get("fingerprint") or "") if isinstance(block, dict) else ""
        if fingerprint and carries_fingerprint(page, bid, fingerprint):
            pool.setdefault(fingerprint, []).append(bid)
    order = page["block_order"]
    assigned: dict[str, str] = {}
    for bid in order:
        target = next((old for old in pool.get(page["blocks"][bid]["fingerprint"], [])
                       if old not in assigned.values()), None)
        if target:
            assigned[bid] = target
    taken = set(assigned.values()) | set(prior.get("block_aliases") or {})
    for bid in order:
        if bid in assigned:
            continue
        assigned[bid] = bid if bid not in taken else fresh_block_id(page["id"], page["blocks"][bid]["fingerprint"],
                                                                    taken | set(order))
        taken.add(assigned[bid])
    if all(old == new for old, new in assigned.items()):
        return
    page["blocks"] = {assigned[bid]: dict(page["blocks"][bid], id=assigned[bid]) for bid in order}
    page["block_order"] = [assigned[bid] for bid in order]
    for link in page.get("links") or []:
        if link.get("block_id") in assigned:
            link["block_id"] = assigned[link["block_id"]]


def carry_over(prior: dict[str, Any], page: dict[str, Any]) -> None:
    """What `ingest --update` keeps from the page it replaces: the ids of blocks it did not
    change, their judgements (resolution) and body-external data (a conflict's claims),
    aliases of blocks that are still there, and the block anchors of curated links the
    new frontmatter declares again."""
    keep_block_ids(prior, page)
    live = set(page["blocks"])
    prior_blocks = prior.get("blocks") or {}
    for bid, block in page["blocks"].items():
        old = prior_blocks.get(bid)
        if not isinstance(old, dict) or old.get("kind") != block.get("kind"):
            continue
        if "resolution" in old:
            block["resolution"] = old["resolution"]
        if block["kind"] in QUOTED_FREE_KINDS and isinstance(old.get("data"), dict):
            block["data"] = dict(old["data"], text=block["data"].get("text"))
    aliases = {old: new for old, new in {**(prior.get("block_aliases") or {}),
                                         **(page.get("block_aliases") or {})}.items()
               if new in live and old not in live}
    if aliases:
        page["block_aliases"] = dict(sorted(aliases.items()))
    anchors = {(link.get("kind"), link_key(str(link.get("target") or ""))): link["block_id"]
               for link in prior.get("links") or []
               if link.get("kind") in ("supersedes", "related") and link.get("block_id") in live}
    for link in page["links"]:
        key = (link.get("kind"), link_key(str(link.get("target") or "")))
        if not link.get("block_id") and key in anchors:
            link["block_id"] = anchors[key]


# --------------------------------------------------------------------------- edit
def write_document(ws: Workspace, rel: str, page: dict[str, Any]) -> None:
    """Write one page back to its shard; a shard holding several pages keeps the others."""
    path = ws.root / rel
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, list):
        index = next((i for i, item in enumerate(value)
                      if isinstance(item, dict) and item.get("id") == page["id"]), None)
        if index is None:
            raise WikiError(f"{rel} no longer holds {page['id']}")
        value[index] = page
    else:
        value = page
    dump(path, value, pretty=True)


def relink(page: dict[str, Any], remap: dict[str, str]) -> list[dict[str, Any]]:
    """`links` again from the body: wiki links come from the blocks' `[[…]]`.

    A wiki link is a `[[link]]` in some block, so those are regenerated; the
    curated kinds (source, supersedes, related) stay, re-pointed at the new
    block id when the block they anchor to was replaced.
    """
    body = page_links(page["id"], page["blocks"], page["block_order"])
    curated = []
    for link in page.get("links") or []:
        if link.get("kind", "wiki") == "wiki":
            continue
        link = dict(link)
        if link.get("block_id") in remap:
            link["block_id"] = remap[link["block_id"]]
        curated.append(link)
    return body + curated


def swap_block(page: dict[str, Any], old_id: str, news: list[dict[str, Any]],
               heir: str = "") -> dict[str, Any]:
    """The page with `news` in `old_id`'s place, in order; the old id becomes an alias of `heir`
    (the first new block unless told otherwise)."""
    ids = [block["id"] for block in news]
    heir = heir or ids[0]
    out = dict(page)
    blocks: dict[str, Any] = {}
    for bid, block in page["blocks"].items():
        if bid == old_id:
            blocks.update((new["id"], new) for new in news)
        else:
            blocks[bid] = block
    out["blocks"] = blocks
    out["block_order"] = [item for bid in page["block_order"] for item in (ids if bid == old_id else [bid])]
    # Flat table: every older id points straight at the live one, and an id
    # that is live again (an edit back to earlier content) is no alias.
    aliases = dict(page.get("block_aliases") or {})
    if old_id not in ids:
        aliases = {old: (heir if live == old_id else live) for old, live in aliases.items()}
        aliases[old_id] = heir
    for bid in ids:
        aliases.pop(bid, None)
    if aliases:
        out["block_aliases"] = dict(sorted(aliases.items()))
    else:
        out.pop("block_aliases", None)
    # 상충 판정이 현행 block 을 가리키고 있었으면(`resolution.current`) 그 가리킴도 새 id 로 잇는다.
    for bid, block in blocks.items():
        current = (block.get("resolution") or {}).get("current") if isinstance(block, dict) else None
        if current and current not in blocks and aliases.get(current) in blocks:
            blocks[bid] = {**block, "resolution": {**block["resolution"], "current": aliases[current]}}
    return out


def follow_current(page: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    """block 의 `resolution.current` 가 옛 id 면 지금 id 로 바꾼 사본 (정본은 그대로)."""
    resolution = block.get("resolution") if isinstance(block.get("resolution"), dict) else None
    current = (resolution or {}).get("current")
    live = live_block_id(page, current) if current else None
    if not current or not live or live == current:
        return block
    return {**block, "resolution": {**resolution, "current": live}}


def fresh_block_id(page_id: str, fingerprint: str, taken: set[str]) -> str:
    """`block:<slug>:<fingerprint>:<n>` with the smallest n not in `taken`."""
    occurrence = 1
    while block_id_of(page_id, fingerprint, occurrence) in taken:
        occurrence += 1
    return block_id_of(page_id, fingerprint, occurrence)


def carries_fingerprint(page: dict[str, Any], block_id: str, fingerprint: str) -> bool:
    prefix = block_id_of(page["id"], fingerprint, 0)[:-1]
    return block_id.startswith(prefix) and block_id[len(prefix):].isdigit()


def edit(ws: Workspace, selector: str, block: str, fragment: str, *,
         dry_run: bool = False) -> dict[str, Any]:
    """Rewrite one block from a markdown fragment; everything derived from it follows.

    source_text, data, fingerprint and refs come from the fragment, and the
    page's wiki `links` are regenerated from the body. When the content changes
    the block takes the id of its new fingerprint; the old id stays in the
    page's `block_aliases`, so `get` keeps finding it.

    A fragment of several blocks goes into the same place, in order — that is
    how a block is split or a new one inserted after it. The old id stays with
    the block whose content it still names, otherwise with the first one.
    """
    rel, page = find_document(ws, selector)
    old_id = live_block_id(page, norm(block))
    if old_id is None:
        old_id = find_block(ws, norm(block), page)[1]["id"]
    label = f"{page['id']}#{old_id}"
    text = str(fragment or "").strip("\n")
    assert_no_secrets(text, label)
    parsed, order = parse_blocks(page["id"], text)
    if not order:
        raise WikiError(f"{label}: 조각이 비었다 — edit 는 block 을 지우지 않는다")
    old = page["blocks"][old_id]
    news = [parsed[bid] for bid in order]
    for new in news:
        lines = new["source_text"].split("\n")
        if new["kind"] == "code" and not (len(lines) > 1 and lines[-1].startswith("```")):
            raise WikiError(f"{label}: 닫히지 않은 ``` 가 있다 — 다음에 page 를 다시 읽으면 뒤의 block 을 code 로 삼킨다")
    # 옛 id 는 내용이 같은 block, 없으면 kind 가 같은 block, 그것도 없으면 첫 block 이 잇는다
    heir = next((new for new in news if new["fingerprint"] == old.get("fingerprint")), None) \
        or next((new for new in news if new["kind"] == old.get("kind")), news[0])
    if heir["kind"] == old.get("kind"):
        if "resolution" in old:
            heir["resolution"] = old["resolution"]  # 판정은 본문에서 나오지 않는다 — 고쳐 쓴다고 풀리지 않는다
        if heir["kind"] in QUOTED_FREE_KINDS and isinstance(old.get("data"), dict):
            heir["data"] = dict(old["data"], text=heir["data"]["text"])      # 주장 목록 같은 본문 밖 data
    taken = set(page["blocks"]) - {old_id}
    heir["id"] = old_id if carries_fingerprint(page, old_id, heir["fingerprint"]) \
        else fresh_block_id(page["id"], heir["fingerprint"], taken)
    taken.add(heir["id"])
    for new in news:
        if new is not heir:
            new["id"] = fresh_block_id(page["id"], new["fingerprint"], taken)
            taken.add(new["id"])
    ids = [new["id"] for new in news]
    result: dict[str, Any] = {"page_id": page["id"], "block_id": heir["id"], "previous": old_id,
                              "dest": rel, "changed": news != [old], "dry_run": dry_run}
    if len(news) > 1:
        result["inserted"] = [bid for bid in ids if bid != heir["id"]]
    if news == [old]:
        return result

    updated = swap_block(page, old_id, news, heir["id"])
    updated["links"] = relink(updated, {old_id: heir["id"]})
    declared = {link_key(l.get("target", "")) for l in page.get("links") or [] if l.get("kind", "wiki") == "wiki"}
    kept = {link_key(l["target"]) for l in updated["links"] if l["kind"] == "wiki"}
    if declared - kept:
        result["dropped_links"] = sorted(declared - kept)
    title = h1_text(page)
    if title is not None and page.get("title") == title and h1_text(updated) not in (None, title):
        updated["title"] = result["title"] = h1_text(updated)    # 제목이 H1 에서 나온 page 는 H1 을 따른다
    stamp = today()
    updated["updated"] = stamp
    note = old_id if ids == [old_id] else f"{old_id} → {', '.join(ids)}"
    updated["history"] = [*page.get("history", []),
                          {"at": stamp, "action": "edited", "actor": "llmwiki-cli", "note": note}]
    errors = validate_page(updated, SchemaValidator.load(ws))
    if errors:
        raise WikiError("\n".join(errors))
    if dry_run:
        result["block"] = heir
        if len(news) > 1:
            result["inserted_blocks"] = [new for new in news if new is not heir]
        return result
    write_document(ws, rel, updated)
    append_log(ws, {"action": "edit", "page_id": page["id"], "block_id": heir["id"],
                    "previous": old_id, "dest": rel})
    result["build"] = build(ws, changed=[rel])
    return result


def h1_text(page: dict[str, Any]) -> str | None:
    """The first level-1 heading — where `ingest` takes a page's title from."""
    for bid in page.get("block_order") or []:
        block = page["blocks"].get(bid) or {}
        if block.get("kind") == "heading" and block.get("data", {}).get("level") == 1:
            return block["data"].get("text")
    return None


def read_fragment(text: str | None, file: str | None) -> str:
    """The `edit` fragment: --text, --file PATH, or stdin (also `--file -`)."""
    if text is not None:
        return text
    if file and file != "-":
        return Path(file).read_text(encoding="utf-8")
    if file is None and sys.stdin.isatty():
        raise WikiError("edit: 바꿀 markdown 조각을 --text, --file 또는 stdin 으로 준다")
    return sys.stdin.read()


# --------------------------------------------------------------------------- repair
# block 이 마지막으로 정본에서 다시 만들어진 기록. 이 뒤의 updated·history 가 '그 뒤에 바뀌었다' 는 표시다.
BASE_ACTIONS = frozenset({"ingested", "created", "edited", "repaired"})


def render_data(page_id: str, kind: str, data: Any) -> str | None:
    """`data` 로 그대로 다시 파싱되는 markdown — 그런 글을 만들 수 없으면 None."""
    if not isinstance(data, dict):
        return None
    text = data.get("text")
    candidates: list[str] = []
    if kind == "heading" and isinstance(data.get("level"), int) and isinstance(text, str):
        candidates.append("#" * data["level"] + " " + text)
    elif kind == "paragraph" and isinstance(text, str):
        candidates.append(text)
    elif kind in ("quote", "open_question") and isinstance(text, str):
        candidates.append("\n".join(f"> {line}".rstrip() for line in text.split("\n")))
    elif kind == "list":
        if isinstance(text, str):
            candidates.append(text)
        items = data.get("items")
        if isinstance(items, list) and all(isinstance(item, str) for item in items):
            candidates.append("\n".join(f"{n}. {item}" if data.get("ordered") else f"- {item}"
                                        for n, item in enumerate(items, 1)))
    elif kind == "table" and isinstance(data.get("rows"), list):
        rows = data["rows"]
        if all(isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in rows):
            candidates.append("\n".join("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
                                         for row in rows))
    elif kind == "code" and isinstance(text, str):
        candidates.append(f"```{data.get('language') or ''}\n{text}\n```")
    elif kind == "thematic_break":
        candidates.append("---")
    for raw in candidates:
        parsed = parse_one(page_id, raw)
        if parsed and parsed["kind"] == kind and parsed["data"] == data:
            return raw
    return None


def snapshot_blocks(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """ingest 때 남긴 원문(`source_snapshot`) 의 block — 손대기 전의 모양이다."""
    snapshot = page.get("source_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("format") != "markdown" \
            or not isinstance(snapshot.get("text"), str):
        return {}
    return parse_blocks(page["id"], split_frontmatter(snapshot["text"])[1])[0]


def change_recorded(page: dict[str, Any]) -> bool:
    """마지막으로 정본에서 다시 만든 뒤(ingest·생성·edit·repair) page 가 바뀌었다고 적혀 있는가.

    `updated` 가 그 기록의 날짜보다 새거나, 그 기록 뒤에 history 가 더 붙었으면 그렇다.
    """
    history = [h for h in page.get("history") or [] if isinstance(h, dict)]
    base = max((i for i, h in enumerate(history) if h.get("action") in BASE_ACTIONS), default=-1)
    base_date = str(history[base].get("at") or "")[:10] if base >= 0 else str(page.get("created") or "")
    return base < len(history) - 1 or str(page.get("updated") or "") > base_date


def rebuilt_block(page: dict[str, Any], block: dict[str, Any], raw: str) -> dict[str, Any]:
    """`raw` 를 정본으로 다시 만든 block (id 는 호출자가 정한다).

    판정(resolution)과 conflict·current 의 본문 밖 data(주장 목록 같은 것)는 본문에서 나오지 않아 그대로 잇는다.
    """
    kind = block["kind"]
    if kind in DATA_FREE_KINDS:
        new = dict(block, refs=refs_in(raw), source_text=raw, fingerprint=block_fingerprint(kind, raw))
        parsed = parse_one(page["id"], raw)
        if kind in QUOTED_FREE_KINDS and isinstance(block.get("data"), dict) and parsed and parsed["kind"] == kind:
            new["data"] = dict(block["data"], text=parsed["data"]["text"])
    else:
        new = dict(parse_one(page["id"], raw) or {})
        if new.get("kind") == kind and "resolution" in block:
            new["resolution"] = block["resolution"]
    return new


def quoted_markdown(page_id: str, kind: str, text: Any) -> str | None:
    """conflict·current 의 data.text 를 같은 kind·같은 text 로 다시 읽히는 인용으로."""
    if not isinstance(text, str):
        return None
    raw = "\n".join(f"> {line}".rstrip() for line in text.split("\n"))
    parsed = parse_one(page_id, raw)
    return raw if parsed and parsed["kind"] == kind and parsed["data"].get("text") == text else None


TABLE_RULE_CELL = re.compile(r":?-+:?")
QUOTE_MARKS = {"conflict": CONFLICT_MARK, "current": CURRENT_MARK}


def same_content(kind: str, parsed: dict[str, Any] | None, data: Any) -> bool:
    """손으로 쓴 data 가 source_text 와 같은 내용을 다른 모양으로 적었는가 — 구분 행(`| --- |`)을 뺀 표,
    items 만 적은 목록. 그렇다면 source_text 에서 다시 만들어도 잃는 것이 없다."""
    if not parsed or parsed["kind"] != kind or not isinstance(data, dict):
        return False
    mine = parsed["data"]
    if kind == "table":
        rows = list(mine["rows"])
        if len(rows) > 1 and rows[1] and all(TABLE_RULE_CELL.fullmatch(cell.strip()) for cell in rows[1]):
            del rows[1]
        return set(data) == {"rows"} and data["rows"] == rows
    if kind == "list":
        return "items" in data and set(data) <= set(mine) and all(data[key] == mine[key] for key in data)
    return False


def marked_quote(page_id: str, kind: str, parsed: dict[str, Any] | None, data: Any) -> str | None:
    """표지 없이 적힌 conflict·current — 본문이 data.text 와 같은 문단(또는 표지 없는 인용)이면 그 글에
    kind 의 표지를 붙인 인용(`> ⚠️ 상충: …`). 그래도 같은 kind 로 다시 읽히지 않으면 None."""
    mark = QUOTE_MARKS.get(kind)
    text = data.get("text") if isinstance(data, dict) else None
    if not mark or not isinstance(text, str) or mark in text or not parsed \
            or parsed["kind"] not in ("paragraph", "quote") or parsed["data"].get("text") != text:
        return None
    raw = "\n".join(f"> {line}".rstrip() for line in f"{mark}: {text}".split("\n"))
    found = parse_one(page_id, raw)
    return raw if found and found["kind"] == kind else None


def original_fingerprint(page: dict[str, Any], bid: str, block: dict[str, Any]) -> str:
    """block 이 마지막으로 정본에서 만들어질 때의 지문.

    정식 id(`block:<slug>:<16hex>:<n>`) 가 품은 지문을 믿는다 — fingerprint 필드는 validate 가 맞는 값을
    알려 주므로 손으로 고쳐졌을 수 있다. 정식 id 가 아니면 그 필드뿐이다.
    """
    found = re.fullmatch(re.escape(f"block:{page['id'].removeprefix('page:')}:") + r"([0-9a-f]{16}):[1-9][0-9]*", bid)
    return found.group(1) if found else str(block.get("fingerprint") or "")


def drift_diff(source_text: str, data: Any, rendered: str | None) -> list[str]:
    other = rendered if rendered is not None else json.dumps(data, ensure_ascii=False, indent=1)
    return [redact(line) for line in difflib.unified_diff(
        source_text.splitlines(), other.splitlines(), "source_text", "data", lineterm="", n=1)]


def diagnose_block(page: dict[str, Any], bid: str, block: dict[str, Any],
                   snapshot: dict[str, dict[str, Any]], recorded: bool) -> dict[str, Any] | None:
    """어긋난 block 하나의 진단: 무엇이 어긋났고, 어느 쪽이 새것이며, 고친 block 은 무엇인가.

    source_text 와 data 가 서로 맞으면 지문·refs·id 만 다시 만든다. 둘이 다르면 원본(정식 id 가 품은 지문이나
    ingest snapshot 과 같은 쪽)을 가리고, 그 뒤의 변경이 updated·history 에 적혀 있을 때만 다른 쪽을 새것으로
    본다. 둘 다 원본 그대로인데 어긋났으면 파서가 바뀐 것이라 source_text 에서 다시 읽는다. 그 밖에는 고치지
    않는다(`new` 가 None). conflict·current 는 data 의 text 만 본문에서 나온 것으로 맞춰 본다.

    판정이 필요 없는 어긋남이 둘 더 있다 — 예전 지침대로 JSON 을 손으로 쓴 page 에 흔하다. data 가 같은 내용을
    다른 모양으로 적었으면(구분 행을 뺀 표, items 만 적은 목록) source_text 에서 다시 만들고(`shape`), 표지 없이
    문단으로 적힌 conflict·current 는 같은 글에 표지를 붙인 인용으로 쓴다(`marker`).
    """
    problems = block_drift(page, bid, block)
    if not problems:
        return None
    kind, text, data = block["kind"], block["source_text"], block.get("data")
    out: dict[str, Any] = {"page_id": page["id"], "block_id": bid, "problems": problems, "diff": [],
                           "verdict": "undecided", "new": None}
    parsed = parse_one(page["id"], text)
    quoted = kind in QUOTED_FREE_KINDS
    if kind in DATA_FREE_KINDS and not quoted:            # raw·markdown: 파서가 만들지 않아 맞춰 볼 data 가 없다
        agree, rendered = True, None
    elif quoted:
        mine = data.get("text") if isinstance(data, dict) else None
        agree = not isinstance(mine, str) or bool(parsed and parsed["kind"] == kind and parsed["data"].get("text") == mine)
        rendered = quoted_markdown(page["id"], kind, mine)
    else:
        agree = bool(parsed and parsed["kind"] == kind and parsed["data"] == data)
        rendered = render_data(page["id"], kind, data)
    if agree:
        out.update(verdict="derived", reason="source_text 와 data 는 같다 — 지문·refs·id 만 다시 만든다",
                   new=rebuilt_block(page, block, text))
        return out
    if same_content(kind, parsed, data):
        out.update(diff=drift_diff(text, data, rendered), verdict="shape",
                   reason="data 가 같은 내용을 다른 모양으로 적었다(구분 행을 뺀 표·items 만 적은 목록) — "
                          "source_text 에서 다시 만든다",
                   new=rebuilt_block(page, block, text))
        return out
    marked = marked_quote(page["id"], kind, parsed, data) if quoted else None
    if marked is not None:
        out.update(diff=drift_diff(text, data, marked), verdict="marker",
                   reason=f"본문에 인용 표지가 없어 {kind} 로 다시 읽히지 않는다 — 같은 글을 표지를 붙인 인용으로 쓴다",
                   new=rebuilt_block(page, block, marked))
        return out
    if kind == "table" and parsed and parsed["kind"] == "table" and data == {"rows": legacy_table_rows(text)}:
        out.update(diff=drift_diff(text, data, rendered), verdict="parser",
                   reason="data 는 예전 파서가 이 source_text 를 읽은 그대로다 — 칸 안의 `\\|` 를 다시 읽는다",
                   new=rebuilt_block(page, block, text))
        return out
    out["diff"] = drift_diff(text, data, rendered)
    witness = original_fingerprint(page, bid, block)
    original = snapshot.get(bid)
    same_data = (lambda a, b: isinstance(a, dict) and isinstance(b, dict) and a.get("text") == b.get("text")) \
        if quoted else (lambda a, b: a == b)
    source_is_original = block_fingerprint(kind, text) == witness or bool(
        original and original.get("source_text") == text)
    data_is_original = bool(rendered is not None and block_fingerprint(kind, rendered) == witness) or bool(
        original and same_data(original.get("data"), data))
    rereadable = parsed is not None and (not quoted or parsed["kind"] == kind)
    if source_is_original and parsed and kind in QUOTE_KINDS and parsed["kind"] in QUOTE_KINDS \
            and parsed["kind"] != kind and isinstance(data, dict) and data.get("text") == parsed["data"].get("text"):
        # 인용의 표지(⚠️ 상충·✅ 현행·❓ 미결) 를 읽는 법만 바뀌었다 — 본문도 data.text 도 그대로다
        out.update(verdict="parser", reason=f"파서가 이 인용을 {parsed['kind']} 로 읽는다 — 본문은 그대로라 다시 읽는다",
                   new=dict(parsed))
        return out
    if source_is_original and data_is_original:
        if rereadable:
            out.update(verdict="parser", reason="둘 다 만들어진 그대로다 — 파서가 바뀌어 source_text 를 다시 읽는다",
                       new=rebuilt_block(page, block, text))
        else:
            out["reason"] = "둘 다 만들어진 그대로지만 source_text 가 같은 block 으로 다시 읽히지 않는다"
    elif not source_is_original and not data_is_original:
        out["reason"] = "어느 쪽이 원본인지 알 수 없다 — id 의 지문도 ingest snapshot 도 둘 다와 다르다"
    elif not recorded:
        out["reason"] = (f"{'data' if source_is_original else 'source_text'} 가 원본과 다르지만 "
                         "updated·history 에 그 뒤의 변경 기록이 없다")
    elif source_is_original and rendered is None:
        out["reason"] = "data 가 새것이지만 같은 data 로 읽히는 markdown 을 만들 수 없다"
    elif source_is_original:
        out.update(verdict="data", reason="data 가 새것이다 — source_text 를 data 에서 다시 쓴다",
                   new=rebuilt_block(page, block, rendered))
    elif not rereadable:
        out["reason"] = "source_text 가 새것이지만 같은 kind 의 block 하나로 읽히지 않는다"
    else:
        out.update(verdict="source_text", reason="source_text 가 새것이다 — data 를 다시 파싱한다",
                   new=rebuilt_block(page, block, text))
    return out


def mend_links(page: dict[str, Any], remap: dict[str, str], rederived: set[str]) -> list[dict[str, Any]]:
    """block id 를 새 id 로 잇고, `links` 에 빠진 본문 `[[링크]]` 를 더한다.

    다시 만든 block(`rederived`) 에 달린 wiki link 는 그 block 의 새 본문을 따른다 — 옛 본문에서 나온
    link 다. 그 밖의 link 는 지우지 않는다.
    """
    links = []
    for link in page.get("links") or []:
        link = dict(link)
        if link.get("block_id") in remap:
            link["block_id"] = remap[link["block_id"]]
        if link.get("kind", "wiki") == "wiki" and link.get("block_id") in rederived:
            refs = {link_key(ref) for ref in refs_in(page["blocks"][link["block_id"]]["source_text"])}
            if link_key(str(link.get("target") or "")) not in refs:
                continue
        links.append(link)
    declared = {link_key(str(l.get("target") or "")) for l in links if l.get("kind", "wiki") == "wiki"}
    for link in page_links(page["id"], page["blocks"], page["block_order"]):
        if link_key(link["target"]) not in declared:
            declared.add(link_key(link["target"]))
            links.append(link)
    return links


def repair(ws: Workspace, *, dry_run: bool = False) -> dict[str, Any]:
    """`validate` 의 drift 를 판정할 수 있는 만큼 고친다. 판정할 수 없는 것은 목록으로만 남긴다.

    tag 의 여러 표기(`pre_v0_1-1`·`pre-v0.1-1`) 도 한 표기로 모은다 — 허용 목록의 표기, 없으면 정본에서
    가장 많이 쓴 표기다. 이것도 판정이 필요 없는 일이다.
    """
    validator = SchemaValidator.load(ws)
    fixed: list[dict[str, Any]] = []
    undecided: list[dict[str, Any]] = []
    pending: list[tuple[str, dict[str, Any], int]] = []
    docs = ws.load_documents()
    vocabulary = ws.load_tag_vocabulary()
    spellings = tag_spellings((page for _rel, page in docs), vocabulary)
    allowed = {tag_key(tag) for tag in vocabulary} if vocabulary is not None else None
    for rel, page in docs:
        tags, keywords = sorted_tags(page, spellings, allowed)
        retagged = tags != page.get("tags") or keywords != string_list(page.get("keywords"))
        drifted = bool(consistency_errors(page))
        if not drifted and not retagged:
            continue
        work, remap, rederived, count = page, {}, set(), 0
        if retagged:
            moved = [word for word in keywords if word not in string_list(page.get("keywords"))]
            raised = [word for word in tags if tag_key(word) not in {tag_key(t) for t in string_list(page.get("tags"))}]
            problems = [f"tags {len(string_list(page.get('tags')))} → {len(tags)}: {tags[:8]}"
                        + (" …" if len(tags) > 8 else "")]
            if moved or raised:
                problems.append(f"keywords 로 {len(moved)}개, tag 로 {len(raised)}개")
            fixed.append({"page_id": page["id"], "block_id": "", "problems": problems, "diff": [], "verdict": "tags",
                          "reason": "tag 의 여러 표기를 한 표기로 모으고, 허용 목록 밖의 tag 는 keywords 로 옮긴다"})
            work = dict(work, tags=tags)
            if keywords:
                work["keywords"] = keywords
            else:
                work.pop("keywords", None)
        snapshot, recorded = snapshot_blocks(page), change_recorded(page)
        for bid in list(page["block_order"]) if drifted else []:
            found = diagnose_block(work, bid, work["blocks"][bid], snapshot, recorded)
            if found is None:
                continue
            new = found.pop("new")
            if new is None:
                undecided.append(found)
                continue
            new["id"] = bid if carries_fingerprint(work, bid, new["fingerprint"]) \
                else fresh_block_id(work["id"], new["fingerprint"], set(work["blocks"]) - {bid})
            work = swap_block(work, bid, [new])
            if new["id"] != bid:
                remap[bid] = new["id"]
            rederived.add(new["id"])
            found["new_id"] = new["id"]
            fixed.append(found)
            count += 1
        if drifted:
            work = dict(work, links=mend_links(work, remap, rederived))
        for problem in consistency_errors(work):
            if problem.startswith(f"{page['id']}: wiki link"):
                undecided.append({"page_id": page["id"], "block_id": "", "problems": [problem], "diff": [],
                                  "verdict": "undecided",
                                  "reason": "본문에 없는 wiki link 는 지우지 않는다 — edit 로 본문에 넣거나 "
                                            "kind 를 related 로 바꾼다"})
        if work != page:
            work["history"] = [*page.get("history", []),
                               {"at": today(), "action": "repaired", "actor": "llmwiki-cli",
                                "note": f"{count} block(s) re-derived from source_text/data"
                                        + ("; tags normalised" if work.get("tags") != page.get("tags") else "")
                                        + ("; off-list tags moved to keywords"
                                           if work.get("keywords") != page.get("keywords") else "")}]
            errors = validate_page(work, validator)
            if errors:
                raise WikiError("\n".join(errors))
            pending.append((rel, work, count))
    report: dict[str, Any] = {"dry_run": dry_run, "fixed": fixed, "undecided": undecided,
                              "pages": [work["id"] for _rel, work, _n in pending]}
    if not ws.fixtures:
        report["pending_file"] = write_repair_pending(ws, undecided)
    if dry_run or not pending:
        return report
    for rel, work, count in pending:
        write_document(ws, rel, work)
        append_log(ws, {"action": "repair", "page_id": work["id"], "blocks": count, "dest": rel})
    report["build"] = build(ws, changed=sorted({rel for rel, _w, _n in pending}))
    return report


REPAIR_PENDING = "repair-pending.json"


def write_repair_pending(ws: Workspace, undecided: list[dict[str, Any]]) -> str:
    """판정할 수 없어 고치지 않은 block 목록을 `.llmwiki/repair-pending.json` 에 남긴다(정본이 아니라 보고서다).
    남은 것이 없으면 지난 목록을 지운다. 돌려주는 값은 그 파일의 root 상대경로(없으면 빈 문자열)."""
    path = ws.layout.derived / REPAIR_PENDING
    if not undecided:
        path.unlink(missing_ok=True)
        return ""
    items = [{"page_id": item["page_id"], "block_id": item["block_id"], "reason": item["reason"],
              "problems": item["problems"], "diff": item["diff"]} for item in undecided]
    dump(path, {"at": today(), "count": len(items), "items": items}, pretty=True)
    return ws.rel(path)


def print_repair(report: dict[str, Any]) -> None:
    for item in report["fixed"] + report["undecided"]:
        where = f"{item['page_id']}#{item['block_id']}" if item["block_id"] else item["page_id"]
        status = "고침" if item["verdict"] != "undecided" else "판정 불가"
        print(f"{where}  {status} — {item['reason']}")
        for problem in item["problems"]:
            print(f"  · {problem}")
        for line in item["diff"]:
            print(f"  {line}")
        if item.get("new_id") and item["new_id"] != item["block_id"]:
            print(f"  → {item['new_id']}")
    if report["undecided"]:
        print(f"판정할 수 없는 {len(report['undecided'])}건 (고치지 않았다):")
        for item in report["undecided"]:
            print(f"  {item['page_id']}#{item['block_id']}" if item["block_id"] else f"  {item['page_id']}")
    tail = " (dry-run — 정본은 아무것도 쓰지 않았다)" if report["dry_run"] else ""
    print(f"{len(report['fixed'])} fixed, {len(report['undecided'])} undecided{tail}")
    if report.get("pending_file"):
        print(f"판정 불가 목록: {report['pending_file']} — edit 로 고친다")


# --------------------------------------------------------------------------- search
def open_search_index(ws: Workspace) -> "search_index.Index":
    """The built index when it is fresh, otherwise one built in memory from the canonical pages.

    `query` therefore never answers from a stale index: freshness means the index's
    `meta.revision` matches `index/revision.json` and no canonical file is newer than it.
    """
    glossary = search_index.load_glossary(ws.root)
    if not ws.fixtures and ws.search_db.is_file():
        try:
            idx = search_index.open_ro(ws.search_db, glossary=glossary)
            fresh = (idx.revision and idx.revision == search_index.read_revision(ws.root)
                     and search_index.newest_mtime(ws.pages_dir) <= ws.search_db.stat().st_mtime)
            if fresh:
                return search_index.configure(idx, ws.root)
            idx.close()
        except (OSError, ValueError, sqlite3.Error):
            pass
    return search_index.configure(search_index.build_memory(ws.load_documents(), glossary=glossary), ws.root)


def query(ws: Workspace, text: str, limit: int = 10) -> list[dict[str, Any]]:
    """Rank pages with the search index (fresh on disk, or built in memory from the pages)."""
    idx = open_search_index(ws)
    try:
        result = idx.search(text, k=max(1, limit))
        pages = idx.pages([h.page_id for h in result.hits])
    finally:
        idx.close()
    rows = []
    for hit in result.hits:
        page = pages.get(hit.page_id, {})
        rows.append({"score": round(hit.score, 4), "id": hit.page_id, "slug": page.get("slug", hit.slug),
                     "title": page.get("title", ""), "summary": page.get("summary", ""),
                     "blocks": list(hit.block_ids), "superseded_by": hit.head if hit.head != hit.page_id else None})
    return rows



# --------------------------------------------------------------------------- schema 판
SCHEMA_ERROR = re.compile(r": schema_version must be ")


def data_schema_versions(ws: Workspace) -> list[str]:
    """정본 page 들의 판 (낮은 것부터). page 가 없으면 lock 의 판, 그것도 없으면 빈 목록."""
    versions = {str(page.get("schema_version") or "") for page in ws.load_pages()}
    if not versions:
        lock = P.read_lock(ws.root) or {}
        versions = {str(lock["schema_version"])} if lock.get("schema_version") else set()
    return sorted(versions, key=P.schema_key)


def data_schema_version(ws: Workspace) -> str | None:
    """정본의 스키마 판 — 판이 섞였으면 가장 낮은 것 (아직 덜 옮긴 것이다)."""
    versions = data_schema_versions(ws)
    return versions[0] if versions else None


def schema_gate(ws: Workspace, command: str) -> None:
    """쓰기 명령(ingest·edit·repair)은 정본의 판이 코드와 같을 때만 쓴다. 읽기와 hook 은 이 문을 지나지 않는다."""
    versions = data_schema_versions(ws)
    if not versions or versions == [SCHEMA_VERSION]:
        return
    if P.schema_key(versions[-1]) > P.schema_key(SCHEMA_VERSION):
        raise WikiError(f"{command} 거부: 정본 schema {versions[-1]} 가 코드 schema {SCHEMA_VERSION} 보다 새것이다 — "
                        "코드를 먼저 갱신하라 (`llmwiki update`)")
    raise WikiError(f"{command} 거부: 정본 schema {', '.join(versions)} ≠ 코드 schema {SCHEMA_VERSION} — "
                    "`llmwiki migrate --dry-run` 으로 바뀔 것을 보고 `llmwiki migrate --apply` 로 옮긴 뒤 다시 하라")


def migrate(ws: Workspace, *, apply: bool = False) -> dict[str, Any]:
    """정본을 코드의 스키마 판으로 옮긴다. 기본은 계획만 낸다(dry-run).

    apply 는 바뀐 정본 전체를 `.llmwiki/migrate/wiki` 에 먼저 쓰고, 거기서 읽어 낸 page 로 validate 를
    돌린다. 옮기기 전에 없던 error 가 하나라도 생기면(판이 안 맞는다는 error 는 옮기는 이유이니 빼고 센다)
    원본을 그대로 두고 멈춘다. 통과하면 wiki/ 와 통째로 바꿔 끼우고 lock 의 판을 고친다.
    """
    docs = ws.load_documents()
    versions = sorted({str(page.get("schema_version") or "") for _rel, page in docs}, key=P.schema_key)
    newer = [v for v in versions if P.schema_key(v) > P.schema_key(SCHEMA_VERSION)]
    if newer:
        raise WikiError(f"migrate 거부: 정본 schema {newer[-1]} 가 코드 schema {SCHEMA_VERSION} 보다 새것이다 — "
                        "코드를 먼저 갱신하라 (`llmwiki update`)")
    files: dict[str, list[dict[str, Any]]] = {}
    shapes: dict[str, bool] = {}
    for path in sorted(ws.pages_dir.rglob("*.json")) if ws.pages_dir.exists() else []:
        if path.name.startswith("."):
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        files[ws.rel(path)] = value if isinstance(value, list) else [value]
        shapes[ws.rel(path)] = isinstance(value, list)
    changes: list[dict[str, Any]] = []
    rewritten: dict[str, list[dict[str, Any]]] = {}
    for rel, pages in files.items():
        out = []
        for page in pages:
            try:
                new = M.migrate_page(page)
            except ValueError as exc:
                raise WikiError(f"{rel}: {page.get('id')}: {exc}") from exc
            if new != page:
                changes.append({"page_id": page.get("id"), "file": rel, "diff": M.page_diff(page, new)})
            out.append(new)
        if out != pages:
            rewritten[rel] = out
    lock = P.read_lock(ws.root)
    report: dict[str, Any] = {"dry_run": not apply, "from": versions, "to": SCHEMA_VERSION,
                              "steps": [f"{a}→{b}" for v in versions for a, b, _ in M.chain(v)],
                              "pages_changed": len(changes), "files_changed": len(rewritten),
                              "changes": changes,
                              "lock": (lock or {}).get("schema_version") if lock else None}
    report["steps"] = list(dict.fromkeys(report["steps"]))
    if not apply:
        return report
    if rewritten:
        before = {e for e in lint(ws, docs)[0] if not SCHEMA_ERROR.search(e)}
        stage_root = ws.root / P.DERIVED_DIR / "migrate"
        shutil.rmtree(stage_root, ignore_errors=True)
        stage = stage_root / "wiki"
        stage_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(ws.pages_dir, stage)
        for rel, pages in rewritten.items():
            target = stage / Path(rel).relative_to(ws.rel(ws.pages_dir))
            value: Any = pages if shapes[rel] else pages[0]
            target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged = ws.load_documents(stage)
        after = set(lint(ws, staged)[0])
        fresh = sorted(after - before)
        if fresh:
            shutil.rmtree(stage_root, ignore_errors=True)
            raise WikiError("migrate 중단 — 옮긴 사본이 validate 를 통과하지 못해 원본을 그대로 둔다:\n"
                            + "\n".join(fresh[:20]) + (f"\n… 외 {len(fresh) - 20}건" if len(fresh) > 20 else ""))
        if not ws.fixtures:  # 기록은 정본의 wiki/log.jsonl 에 — 바꿔 끼울 사본에 미리 적어 함께 들어간다
            entry = {"at": timestamp(), "action": "migrate", "from": versions, "to": SCHEMA_VERSION,
                     "pages": len(changes)}
            with (stage / "log.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        old = stage_root / "wiki.old"
        os.replace(ws.pages_dir, old)
        try:
            os.replace(stage, ws.pages_dir)
        except OSError:
            os.replace(old, ws.pages_dir)
            raise
        shutil.rmtree(stage_root, ignore_errors=True)
        report["validated"] = {"errors_before": len(before), "errors_after": len(after)}
    if lock is not None and lock.get("schema_version") != SCHEMA_VERSION:
        P.write_lock(ws.root, {**lock, "schema_version": SCHEMA_VERSION, "updated": today()})
        report["lock"] = SCHEMA_VERSION
    if rewritten and not ws.fixtures:
        report["build"] = build(ws)
    return report


def print_adopt(report: dict[str, Any]) -> None:
    def show(title: str, items: Iterable[str]) -> None:
        items = list(items)
        if items:
            print(f"{title} ({len(items)})")
            for item in items:
                print(f"  {item}")
    print(f"adopt {report['root']} ← 코드 {report['code']}" + ("  [dry-run]" if report["dry_run"] else ""))
    show("지운다 (코드)", report["remove"])
    show("남긴다 — 코드 디렉터리 안이지만 코드 repo 에 없는 이 저장소만의 파일 (옮길 곳은 직접 정한다)", report.get("keep") or [])
    show("옮긴다", (f"{a} → {b}" for a, b in report["move"]))
    show("git 추적에서 뺀다 (파생물)", report["untrack"])
    show("지운다 (파생물 디렉터리)", report["delete_derived"])
    show("git 추적에서 뺀다 (skills 링크 — 기계마다 다시 만든다)", report["untrack_links"])
    show("만든다", report["create"])
    show("다시 만든다 (생성 지침)", report["rewrite"])
    for item in report["local"]["sources"]:
        print(f"LOCAL.md 로: {item['file']} (기준 upstream {item['base'][:12]}) — 옮김 {item['added']}줄, "
              f"검토 필요 {item['review']}곳")
    show("충돌 — apply 전에 정리한다", report["conflicts"])
    show("손대지 않고 남는 것", report["leftover"])
    for item in report.get("code_remotes") or []:
        line = f"remote {item['name']} ({item['url']}) 는 코드 저장소다 — 이름은 그대로 두고, 위키를 그쪽으로 push 하지 않는다"
        if "branch_upstream" in item:
            line += f"; branch upstream → {item['branch_upstream'] or '없음 — 개인 저장소를 붙인 뒤 git push -u'}"
        print(line)
    if report.get("wiki_unchanged"):
        print(f"wiki/ sha256 불변: {report['wiki_unchanged']['files']}개 파일 — raw/ {report['raw_unchanged']['files']}개 그대로")
    for name, item in (report.get("checks") or {}).items():
        extra = f" errors {item['errors']}, warnings {item['warnings']}" if "errors" in item else ""
        print(f"{name}: exit {item['exit']}{extra}")
        for line in item.get("first_errors") or []:
            print(f"  {line}")
    if report.get("commit"):
        print(f"커밋 {report['commit'][:12]}")
    if report["dry_run"]:
        print("(dry-run — 아무것도 바꾸지 않았다. --apply 로 적용)")


def print_migrate(report: dict[str, Any]) -> None:
    for item in report["changes"]:
        print(f"{item['page_id']}  ({item['file']})")
        for line in item["diff"]:
            print(f"  {line}")
    span = ", ".join(report["from"]) or "(page 없음)"
    steps = " · ".join(report["steps"]) or "변환 없음"
    print(f"schema {span} → {report['to']} [{steps}] — {report['pages_changed']} page, "
          f"{report['files_changed']} 파일" + (" (dry-run — 아무것도 쓰지 않았다, --apply 로 적용)"
                                              if report["dry_run"] else " 적용"))


# --------------------------------------------------------------------------- update
def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        raise WikiError(f"{question} — 확인이 필요하다. 터미널에서 다시 하거나 --yes 를 준다")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def update(ws: Workspace, *, yes: bool = False, dry_run: bool = False, post_pull: bool = False,
           prior: str = "") -> int:
    """코드 repo 를 받고 데이터 repo 를 그 코드에 맞춘다 (`llmwiki update`, `install.sh/ps1 update`).

    1) 데이터 repo 가 dirty 하면 멈춘다  2) 코드 repo `git pull --ff-only` (로컬 변경·commit 이 있으면 멈춘다)
    3) 정본과 코드의 schema 가 다르면 migrate dry-run 을 보이고 확인을 받는다  4) migrate --apply
    5) 지침·skills 를 다시 만들고 build·validate·lint — validate error 면 커밋하지 않고 멈춘다
    6) lock 을 고치고 데이터 repo 에 커밋한다  7) 주기 루틴이 push 하도록 설치돼 있을 때만 push 한다.
    2) 다음은 받아 온 새 코드가 해야 하므로 `update --post-pull` 로 새 프로세스를 띄운다.
    """
    code = P.CODE_ROOT
    try:
        if not ws.split:
            return update_in_place(ws, yes=yes, dry_run=dry_run, post_pull=post_pull, prior=prior)
        data = ws.root
        if not post_pull:
            if R.is_own_repo(data):
                R.exclude_nested_code(data, code, dry_run=dry_run)
                changes = R.dirty(data)
                if changes:
                    raise R.UpdateStop(f"데이터 repo 에 커밋되지 않은 변경이 있다 — 먼저 커밋하거나 치운다:\n{changes[:800]}")
            else:
                print("데이터 repo 가 git 저장소가 아니다 — 커밋 없이 맞춘다")
            pulled = R.pull_code(code, dry_run=dry_run)
            if dry_run:
                return update_post(ws, yes=yes, dry_run=True, prior=pulled["before"])
            argv = [sys.executable, str(code / "scripts" / "llmwiki.py"), "--root", str(data), "update",
                    "--post-pull", "--prior-commit", pulled["before"]] + (["--yes"] if yes else [])
            sys.stdout.flush()
            return subprocess.call(argv)
        return update_post(ws, yes=yes, dry_run=dry_run, prior=prior)
    except R.UpdateStop as exc:
        print(f"update 멈춤: {exc}", file=sys.stderr)
        return 1


def update_in_place(ws: Workspace, *, yes: bool, dry_run: bool, post_pull: bool, prior: str) -> int:
    """설치한 폴더 하나(코드+위키)를 그 자리에서 갱신한다.

    앞단계: 폴더가 깨끗한지 → upstream 을 받아(숨은 ref) 지난번 기준과 새 판을 이 폴더 코드 파일에 3-way 로
    얹는다(충돌이면 아무것도 쓰지 않고 멈춘다). 뒷단계는 방금 얹은 새 코드가 `update --post-pull` 로 한다:
    migrate → build·validate·lint → 이 폴더 git 에 커밋 → 주기 루틴이 push 하도록 설치돼 있으면 push.
    """
    root = ws.root
    if post_pull:
        return update_in_place_post(ws, yes=yes, dry_run=dry_run, prior=prior)
    if not R.is_own_repo(root):
        raise R.UpdateStop(f"{root} 가 git 저장소가 아니다 — 제자리 갱신은 이 폴더의 git 으로 기준 판을 찾는다")
    if not (root / "scripts" / "llmwiki.py").is_file():
        raise R.UpdateStop(f"{root} 에 scripts/llmwiki.py 가 없다 — 코드와 위키가 한 폴더인 설치가 아니다")
    changes = R.dirty(root)
    if changes:
        raise R.UpdateStop(f"커밋되지 않은 변경이 있다 — 먼저 커밋하거나 치운다:\n{changes[:800]}")
    url = R.upstream_url(root)
    new = R.fetch_upstream(root, url)
    local = R.LocalBlobs(root)
    base, how = R.find_base(root, new, local)
    old_version, new_version = P.code_version(root), R.tree_version(root, new)
    print(f"upstream {url} — 기준 {base[:7]} ({how}) → 새 판 {new[:7]} (v{old_version}→v{new_version})")
    plan = R.plan_sync(root, base, new, local)
    for title, items in (("새 판으로", [rel for rel, _ in plan["write"]]), ("합친다 (양쪽이 고쳤고 겹치지 않는다)",
                         [rel for rel, _, _ in plan["merge"]]), ("지운다 (upstream 이 지웠다)", plan["delete"]),
                         ("남긴다 (이 폴더에서만 고쳤다)", plan["kept"]), ("충돌 — 같은 자리를 양쪽이 고쳤다", plan["conflicts"])):
        if items:
            print(f"{title} ({len(items)})")
            for rel in items[:200]:
                print(f"  {rel}")
    if plan["conflicts"]:
        raise R.UpdateStop(f"충돌 {len(plan['conflicts'])}개 — 아무것도 쓰지 않았다. 위 파일을 upstream({new[:7]}) 쪽과 "
                           "맞춘 뒤(또는 이 폴더의 수정을 되돌려 커밋한 뒤) 다시 update 한다")
    if dry_run:
        print("[dry-run] 코드를 얹은 뒤 migrate · build · validate · lint · 커밋")
        return 0
    R.apply_sync(root, plan)
    R.write_upstream(root, url, new, new_version)
    argv = [sys.executable, str(root / "scripts" / "llmwiki.py"), "--root", str(root), "update", "--post-pull",
            "--prior-commit", f"{base}:{old_version}"] + (["--yes"] if yes else [])
    sys.stdout.flush()
    return subprocess.call(argv)


def update_in_place_post(ws: Workspace, *, yes: bool, dry_run: bool, prior: str) -> int:
    """제자리 갱신의 뒷단계 — 얹은 새 코드로 돈다."""
    root = ws.root
    base, _, old_version = prior.partition(":")
    record = R.read_upstream(root)
    new, new_version = str(record.get("commit") or ""), P.code_version(root)
    versions = data_schema_versions(ws)
    old_schema = versions[0] if versions else SCHEMA_VERSION
    if versions and P.schema_key(versions[-1]) > P.schema_key(SCHEMA_VERSION):
        raise R.UpdateStop(f"정본 schema {versions[-1]} 가 코드 schema {SCHEMA_VERSION} 보다 새것이다")
    if versions and versions != [SCHEMA_VERSION]:
        plan = migrate(ws, apply=False)
        print_migrate(plan)
        if not confirm(f"정본 {plan['pages_changed']} page 를 schema {old_schema}→{SCHEMA_VERSION} 로 옮길까?", yes):
            print("옮기지 않고 멈춘다 — 코드는 얹었고 커밋하지 않았다 (`git checkout -- .` 로 되돌릴 수 있다)")
            return 1
        migrate(ws, apply=True)
        print(f"migrate: {plan['pages_changed']} page 를 schema {SCHEMA_VERSION} 로 옮겼다")
    try:
        build(ws)
        built = ""
    except WikiError as exc:
        built = str(exc)
    errors, warnings = lint(ws)
    print(f"build {'통과' if not built else '실패'} · validate {len(errors)} errors · lint {len(warnings)} warnings")
    if built or errors:
        for line in (errors or [built])[:20]:
            print(f"ERROR {line}")
        print("validate error 가 있어 커밋하지 않고 멈춘다 — 고친 뒤 커밋한다 (얹은 코드는 그대로 있다)", file=sys.stderr)
        return 1
    message = R.update_message(old_version or "0.0.0", new_version, base, new, old_schema, SCHEMA_VERSION)
    sha = R.commit_all(root, message)
    if not sha:
        print(f"이미 최신 — 코드 v{new_version} ({new[:7] or '-'}), schema {SCHEMA_VERSION}")
        return 0
    print(f"커밋 {sha[:7]}: {message}")
    target = R.routine_push_target(root)
    if target and R.read_only_remote(root, target[0]):
        print(f"push 하지 않았다 — remote '{target[0]}' {R.READ_ONLY_PUSH}")
    elif target:
        status, branch = R.git(root, "rev-parse", "--abbrev-ref", "HEAD")
        status, out = R.git(root, "push", target[0], f"HEAD:{branch}", timeout=300.0)
        print(f"push {target[0]}/{branch}: {'완료' if status == 0 else '실패 — ' + out[-200:]}")
    else:
        print("push 하지 않았다 (주기 루틴의 push 가 꺼져 있다)")
    return 0


def update_post(ws: Workspace, *, yes: bool, dry_run: bool, prior: str) -> int:
    """update 의 뒷단계 — 받아 온 코드로 돈다."""
    data, code = ws.root, P.CODE_ROOT
    lock = P.read_lock(data) or {}
    old_version = str(lock.get("code_version") or "0.0.0")
    old_sha = str(lock.get("code_commit") or prior or "")
    new_version, new_sha = P.code_version(code), R.code_commit(code)
    versions = data_schema_versions(ws)
    old_schema = versions[0] if versions else SCHEMA_VERSION
    if versions and P.schema_key(versions[-1]) > P.schema_key(SCHEMA_VERSION):
        raise R.UpdateStop(f"정본 schema {versions[-1]} 가 코드 schema {SCHEMA_VERSION} 보다 새것이다 — "
                           "코드를 먼저 갱신하라 (이 코드 repo 가 upstream 보다 뒤처졌다)")
    needs_migrate = bool(versions) and versions != [SCHEMA_VERSION]
    if (not needs_migrate and old_sha == new_sha and old_version == new_version
            and str(lock.get("code_path") or "") == str(code)):
        print(f"이미 최신 — 코드 v{new_version} ({new_sha[:7] or '-'}), schema {SCHEMA_VERSION}")
        return 0
    if needs_migrate:
        plan = migrate(ws, apply=False)
        print_migrate(plan)
        if dry_run:
            print(f"[dry-run] migrate --apply 로 schema {old_schema}→{SCHEMA_VERSION}")
        elif not confirm(f"정본 {plan['pages_changed']} page 를 schema {old_schema}→{SCHEMA_VERSION} 로 옮길까?", yes):
            print("옮기지 않고 멈춘다 — 아무것도 커밋하지 않았다")
            return 1
        else:
            migrate(ws, apply=True)
            print(f"migrate: {plan['pages_changed']} page 를 schema {SCHEMA_VERSION} 로 옮겼다")
    message = R.update_message(old_version, new_version, old_sha, new_sha, old_schema, SCHEMA_VERSION)
    if dry_run:
        guides = R.sync_guides(data, code, dry_run=True)
        print(f"[dry-run] 지침·skills: {guides}")
        print(f"[dry-run] build · validate · lint 뒤 데이터 repo 커밋: {message}")
        return 0
    R.sync_guides(data, code)
    try:
        build(ws)
        built = ""
    except WikiError as exc:
        built = str(exc)
    errors, warnings = lint(ws)
    print(f"build {'통과' if not built else '실패'} · validate {len(errors)} errors · lint {len(warnings)} warnings")
    if built or errors:
        for line in (errors or [built])[:20]:
            print(f"ERROR {line}")
        print("validate error 가 있어 커밋하지 않고 멈춘다 — 고친 뒤 `llmwiki update` 를 다시 돌린다", file=sys.stderr)
        return 1
    R.refresh_lock(data, schema_version=SCHEMA_VERSION, code=code)
    if not R.is_own_repo(data):
        print(f"갱신했다 (커밋 없음): {message}")
        return 0
    sha = R.commit_data(data, message)
    print(f"데이터 repo 커밋 {sha[:7]}: {message}" if sha else "데이터 repo 에 바뀐 것이 없다 — 커밋하지 않았다")
    target = R.routine_push_target(data)
    if sha and target and R.read_only_remote(data, target[0]):
        print(f"push 하지 않았다 — remote '{target[0]}' {R.READ_ONLY_PUSH}")
    elif sha and target:
        remote = target[0]
        status, branch = R.git(data, "rev-parse", "--abbrev-ref", "HEAD")
        status, out = R.git(data, "push", remote, f"HEAD:{branch}", timeout=300.0)
        print(f"push {remote}/{branch}: {'완료' if status == 0 else '실패 — ' + out[-200:]}")
    elif sha:
        print("push 하지 않았다 (주기 루틴의 push 가 꺼져 있다)")
    return 0

# --------------------------------------------------------------------------- CLI
def emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llmwiki", description=__doc__.splitlines()[0])
    parser.add_argument("--root", help="data root (default: $LLMWIKI_ROOT, then the nearest llmwiki.lock "
                                          "above the current directory, then this repo)")
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser("ingest", help="add one raw source as a canonical page")
    ing.add_argument("input")
    ing.add_argument("--type", choices=sorted(ALLOWED_TYPES))
    ing.add_argument("--project", action="append", default=[])
    ing.add_argument("--summary", default="")
    ing.add_argument("--tag", action="append", default=[])
    ing.add_argument("--slug", default="",
                     help="override the page slug (default: the file stem) — "
                          "use it when several sources share a filename")
    ing.add_argument("--update", action="store_true", help="replace an existing page, keeping created/history")
    ing.add_argument("--dry-run", action="store_true")

    ed = sub.add_parser("edit", help="rewrite one block from a markdown fragment (the old id stays an alias)")
    ed.add_argument("selector", help="page: slug, page:slug or title")
    ed.add_argument("--block", required=True, help="block id (an id an earlier edit replaced works too)")
    fragment = ed.add_mutually_exclusive_group()
    fragment.add_argument("--text", help="the markdown fragment")
    fragment.add_argument("--file", help="read the fragment from this file ('-' = stdin, the default)")
    ed.add_argument("--dry-run", action="store_true")
    ed.add_argument("--fixtures", action="store_true")

    rep = sub.add_parser("repair", help="re-derive drifted blocks where history/updated tell which side is newer")
    rep_mode = rep.add_mutually_exclusive_group()
    rep_mode.add_argument("--dry-run", action="store_true",
                          help="print the diffs and verdicts, write no page (the undecided list still goes "
                               "to .llmwiki/repair-pending.json)")
    rep_mode.add_argument("--apply", action="store_true", help="fix the decidable blocks (the default)")
    rep.add_argument("--json", action="store_true", dest="as_json")
    rep.add_argument("--fixtures", action="store_true")

    for name, help_text in (("build", "regenerate index/ (incl. search.sqlite) and viewer data "
                                      "(under .llmwiki/ in a data repo)"),
                            ("validate", "schema + structural checks"),
                            ("lint", "links, conflicts, orphans, stale index")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--fixtures", action="store_true")
        if name != "build":
            p.add_argument("--json", action="store_true", dest="as_json")
        else:
            p.add_argument("--heading-paths", action="store_true",
                           help="index blocks with their heading path prefixed (H6; default off)")
            p.add_argument("--changed", nargs="+", metavar="PATH", default=None,
                           help="hint: only these wiki files changed (verified by an mtime scan; "
                                "anything else changed falls back to a full build)")
            p.add_argument("--full", action="store_true", help="ignore the work index and rebuild everything")

    q = sub.add_parser("query", help="rank pages with the search index")
    q.add_argument("query")
    q.add_argument("--limit", type=int, default=10)
    q.add_argument("--fixtures", action="store_true")

    get = sub.add_parser("get", help="page or block projection (slug, slug#block:…, block:…)")
    get.add_argument("selector")
    get.add_argument("--block", help="block id within the selected page")
    get.add_argument("--field", action="append", help="descend one key or array index (repeatable)")
    get.add_argument("--pointer", help="RFC 6901 JSON pointer")
    get.add_argument("--fixtures", action="store_true")

    ren = sub.add_parser("render", help="render a page, block or section")
    ren.add_argument("selector")
    ren.add_argument("--format", choices=["md", "html", "json"], default="md")
    ren.add_argument("--exact", action="store_true", help="replay the ingested markdown snapshot")
    ren.add_argument("--section", help="heading block id: render that section only")
    ren.add_argument("--fixtures", action="store_true")

    out = sub.add_parser("outline", help="list heading blocks (cheap section index)")
    out.add_argument("selector")
    out.add_argument("--fixtures", action="store_true")

    mig = sub.add_parser("migrate", help="move the canonical pages to this code's schema version (dry-run by default)")
    mig.add_argument("--dry-run", action="store_true", help="(default) print per-page changes, write nothing")
    mig.add_argument("--apply", action="store_true", help="write to a staging copy, validate it, then swap it in")
    mig.add_argument("--json", action="store_true", dest="as_json")
    mig.add_argument("--fixtures", action="store_true")

    ini = sub.add_parser("init", help="create an empty data repo (wiki/ raw/ config/ LOCAL.md llmwiki.lock) "
                                       "with its first commit")
    ini.add_argument("dir")
    ini.add_argument("--no-git", action="store_true", help="do not git init / commit")

    ado = sub.add_parser("adopt", help="turn an existing single repo into a data repo (dry-run by default)")
    ado.add_argument("dir", nargs="?", help="the repo to convert (default: the resolved data root, e.g. the "
                                            "--data the installer recorded in the llmwiki shim)")
    ado.add_argument("--dry-run", action="store_true", help="(default) list what would be removed, moved, created")
    ado.add_argument("--apply", action="store_true")
    ado.add_argument("--no-commit", action="store_true", help="leave the result uncommitted")
    ado.add_argument("--json", action="store_true", dest="as_json")

    up = sub.add_parser("update", help="one-folder install: lay the new upstream code onto this folder (3-way), "
                                       "then migrate, build/validate/lint, commit. split data repo: pull the code "
                                       "repo (ff-only) and bring the data repo to it")
    up.add_argument("--data", help="data repo (default: the resolved data root)")
    up.add_argument("--yes", "-y", action="store_true", help="do not ask before migrating")
    up.add_argument("--dry-run", action="store_true", help="write nothing (no pull, no migrate, no commit)")
    up.add_argument("--post-pull", action="store_true", help=argparse.SUPPRESS)
    up.add_argument("--prior-commit", default="", help=argparse.SUPPRESS)

    sh = sub.add_parser("shim", help="install/remove/show the user-level `llmwiki` command")
    sh.add_argument("action", choices=("install", "remove", "status"))
    sh.add_argument("--python", default=None, help="interpreter the shim runs (default: this one)")
    sh.add_argument("--data", help="record this data repo as the default data root")
    sh.add_argument("--windows", action="store_true", default=None, help="write the Windows .cmd form")
    sh.add_argument("--posix", action="store_false", dest="windows", help="write the POSIX sh form")
    sh.add_argument("--dry-run", action="store_true")
    sh.add_argument("--field", help="status: print only this field")
    sh.add_argument("--json", action="store_true", dest="as_json")

    gd = sub.add_parser("guides", help="data repo: regenerate CLAUDE.md/AGENTS.md (INIT.md + LOCAL.md) and the "
                                         "skills links (rewrites nothing that is already current)")
    gd.add_argument("--dry-run", action="store_true")

    sub.add_parser("paths", help="print the resolved data root and its layout as JSON (viewer reads this)")

    log = sub.add_parser("log", help="append to or read wiki/log.jsonl")
    log.add_argument("--action")
    log.add_argument("--page")
    log.add_argument("--note")
    log.add_argument("--show", type=int, default=10)
    return parser


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ws = Workspace(args.root, getattr(args, "fixtures", False))
    command = args.command

    if command in {"ingest", "edit", "repair"} and not args.dry_run:
        schema_gate(ws, command)

    if command == "ingest":
        emit(ingest(ws, Path(args.input), args.type, args.project, args.summary,
                    args.update, args.dry_run, args.slug, args.tag))
    elif command == "edit":
        emit(edit(ws, args.selector, args.block, read_fragment(args.text, args.file),
                  dry_run=args.dry_run))
    elif command == "repair":
        report = repair(ws, dry_run=args.dry_run)
        if args.as_json:
            emit(report)
        else:
            print_repair(report)
        return 1 if report["undecided"] else 0
    elif command == "build":
        emit(build(ws, changed=args.changed, full=args.full, heading_paths=args.heading_paths))
    elif command in {"validate", "lint"}:
        errors, warnings = lint(ws)
        counts = issue_counts(ws.load_pages()) if command == "lint" else None
        if command == "validate":
            warnings = []
        if getattr(args, "as_json", False):
            extra = {"counts": counts, "warning_kinds": warning_kinds(warnings)} if counts else {}
            emit({"errors": errors, "warnings": warnings, **extra})
        else:
            for item in errors:
                print("ERROR", item)
            for item in warnings:
                print("WARN", item)
            tally = (f" — 미판정 상충 {counts['unresolved_conflicts']}, 미결 질문 {counts['open_questions']}"
                     if counts else "")
            print(f"{len(errors)} errors, {len(warnings)} warnings{tally}")
        return 1 if errors else 0
    elif command == "query":
        for row in query(ws, args.query, args.limit):
            print(json.dumps(row, ensure_ascii=False))
    elif command == "get":
        selector = f"{args.selector}#{args.block}" if args.block else args.selector
        page, block = resolve(ws, selector)
        value: Any = block if block else page
        if args.pointer:
            value = json_pointer(value, args.pointer)
        value = pick_fields(value, args.field or [])
        emit(value)
    elif command == "render":
        page, block = resolve(ws, args.selector)
        blocks = [block] if block else None
        if args.section:
            blocks = section(page, args.section)
        if args.format == "json":
            emit(blocks if blocks else page)
        elif args.format == "html":
            print("".join(render_block_html(b) for b in blocks) if blocks else render_html(page))
        elif blocks:
            print("\n\n".join(b["source_text"] for b in blocks))
        else:
            print(render_markdown(page, args.exact), end="")
    elif command == "outline":
        page, _ = resolve(ws, args.selector)
        emit(outline(page))
    elif command == "migrate":
        if args.dry_run and args.apply:
            raise WikiError("--dry-run 과 --apply 는 함께 줄 수 없다")
        report = migrate(ws, apply=args.apply)
        if args.as_json:
            emit(report)
        else:
            print_migrate(report)
    elif command == "init":
        try:
            emit(R.init_data_repo(Path(args.dir), use_git=not args.no_git))
        except (ValueError, RuntimeError) as exc:
            raise WikiError(str(exc)) from exc
    elif command == "adopt":
        if args.dry_run and args.apply:
            raise WikiError("--dry-run 과 --apply 는 함께 줄 수 없다")
        target = Path(args.dir) if args.dir else ws.root
        if not args.dir and ws.root == P.CODE_ROOT:
            raise WikiError("adopt 할 저장소를 준다 — `llmwiki adopt <dir>` (설치기를 --data <dir> 로 돌렸으면 생략해도 된다)")
        try:
            report = R.adopt(target, apply=args.apply, commit=not args.no_commit,
                             check=lambda root: R.run_checks(root))
        except (ValueError, RuntimeError) as exc:
            raise WikiError(str(exc)) from exc
        if args.as_json:
            emit(report)
        else:
            print_adopt(report)
        checks = report.get("checks") or {}
        return 1 if any(item.get("errors") for item in checks.values()) else 0
    elif command == "update":
        target = Workspace(args.data) if args.data else ws
        return update(target, yes=args.yes, dry_run=args.dry_run, post_pull=args.post_pull,
                      prior=args.prior_commit)
    elif command == "shim":
        if args.action in ("install", "remove"):
            report = (R.install_shim(args.python or sys.executable, data=Path(args.data) if args.data else None,
                                     windows=args.windows, dry_run=args.dry_run)
                      if args.action == "install" else R.remove_shim(windows=args.windows, dry_run=args.dry_run))
            if args.as_json:
                emit(report)
            else:
                words = {"written": "놓았다", "current": "이미 이 코드를 가리킨다", "would-write": "[dry-run] 놓는다",
                         "kept-foreign": "같은 이름의 남의 파일이 있다 — 덮어쓰지 않는다",
                         "removed": "지웠다", "absent": "없다", "would-remove": "[dry-run] 지운다"}
                print(f"  llmwiki: {report['path']} — {words.get(report['status'], report['status'])}"
                      + (f" (데이터 {report['data']})" if report.get("data") else ""))
                if report.get("hint"):
                    print("  " + report["hint"].replace("\n", "\n  "))
            return 0 if report["status"] != "kept-foreign" else 1
        else:
            state = R.shim_state(args.windows)
            if args.field:
                print(state.get(args.field, ""))
            else:
                emit(state)
    elif command == "guides":
        if not ws.split:
            raise WikiError(f"{ws.root} 는 데이터 repo 가 아니다 ({P.LOCK_NAME} 없음) — 단일 repo 의 CLAUDE.md 는 손으로 둔다")
        emit(R.sync_guides(ws.root, dry_run=args.dry_run))
    elif command == "paths":
        emit({**ws.layout.describe(), "root_source": P.root_source(args.root)})
    elif command == "log":
        if args.action:
            emit(append_log(ws, {k: v for k, v in
                                 (("action", args.action), ("page_id", args.page), ("note", args.note))
                                 if v}))
        else:
            for row in read_log(ws, args.show):
                print(json.dumps(row, ensure_ascii=False))
    return 0


def main() -> None:
    try:
        raise SystemExit(run())
    except (WikiError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
