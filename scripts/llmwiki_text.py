#!/usr/bin/env python3
"""Shared text identity for the CLI, the search index, and the hook.

`link_key`, `implied_links`, and `redact` used to live in two copies that had
already drifted: `[[Page:Alpha]]` resolved in the index and not in the viewer
graph, a bare `sources: [foo]` became an edge in one place only, and ingest
refused fewer credential shapes than the index would mask. One module owns
those rules so the graph, the ranker, and the secret check see the same text.
"""
from __future__ import annotations

import functools
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable

WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#([^\]|]+))?(?:\|([^\]]+))?\]\]")
# `[['AUC-ROC','>=','0.85']]` inside a code span is data, not a link.
INLINE_CODE = re.compile(r"(`+)(?:.|\n)*?\1")
LINK_SEPARATOR = re.compile(r"[\s_]+")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

MASK = "(접속 정보 생략)"
# Every rule is (pattern, name group, value group, secret group). redact replaces
# the value group, so the name, separator and quotes around it keep their shape;
# the secret group is what a placeholder or an earlier mask has to cover. A name
# group means a `name: value` pair, judged by its whole name too. A value stops
# at `&` — a query string holds several pairs, and one exempt pair must not hide
# the next (`csrf_token=abc&password=hunter2`).
# Named credentials: `password: x`, `"api_key": "x"`, `--token=x`, `client_secret = 'x'`.
SECRET = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|"
    r"secret|connection[_-]?string|private[_-]?key|client[_-]?secret|token)"
    r"\"?[ \t]*[:=][ \t]*[\"']?([^\s,;\"'&]+)")              # 값은 같은 줄에 있다 — `password:\n  type: string`
SECRET_MASK = MASK
# An auth header always carries a credential; a bare `basic`/`bearer` in prose
# ("basic authentication", "Basic JavaScript", "basic read/write") only when the
# next word looks like a token (`token_like`).
AUTH_HEADER = re.compile(
    r"(?i)\b(?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})")
AUTH_SCHEME = re.compile(r"(?i)\b(?:bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})")
# A flag starts a word — `access-token 을`, `x-api-key 헤더`, `pre-auth 단계` are prose — and may carry a
# prefix: `--db-password`, `--admin-password`, `--access-token`.
SECRET_FLAG = re.compile(
    r"(?i)(?<![^\s\"'`(\[])--?(?:[a-z0-9]+[-_])*(password|passwd|token|api[_-]?key|secret|auth)\b\s+[\"']?"
    r"([^\s\"'`)\]]+)")
SECRET_RULES = (
    (SECRET, 1, 2, 2),
    # Values that are credentials even without a name — auth headers, PEM,
    # known token prefixes, URL userinfo, CLI flags.
    (AUTH_HEADER, 0, 1, 1),
    (AUTH_SCHEME, 0, 1, 1),
    (re.compile(r"(?s)(-----BEGIN [A-Z ]*PRIVATE KEY-----(.*?)-----END [A-Z ]*PRIVATE KEY-----)"), 0, 1, 2),
    # `sk-learn-tutorials-overview-page` 는 slug 다 — 키는 숫자나 대문자를 품는다.
    (re.compile(r"\b(sk-(?=[A-Za-z0-9_\-]*[0-9A-Z])[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
                r"xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[A-Za-z0-9_\-]{20,})"), 0, 1, 1),
    (re.compile(r"\b[a-z][a-z0-9+.\-]*://([^\s/:@]*:([^\s/@]+))@"), 0, 1, 2),    # `redis://:pa55@host` 도
    (SECRET_FLAG, 1, 2, 2),
)
SECRET_EXTRA = tuple(rule[0] for rule in SECRET_RULES[1:])
# 규칙 어느 것이든 맞으려면 글에 이 조각 중 하나가 있어야 한다. 대부분의 문자열(id, 짧은 본문)은 여기서
# 끝난다 — build 가 page 의 모든 문자열을 훑으므로 10k page 에서 1초 남짓을 아낀다.
SECRET_TRIGGER = re.compile(r"(?i)pass|secret|token|key|connection|auth|bearer|basic|-----begin|sk-|"
                            r"gh[pousr]_|xox[baprs]-|aiza|://")
VALUE_CHAR = re.compile(r"[^\s,;\"'&]")
VALUE_RUN = re.compile(r"[^\s,;\"'&]*")
# `<your token>` 은 자리표시다. 값 자리에서 시작해 값을 덮어야 한다 — 뒤에 붙은 닫는 문장부호(`` ` ``·괄호·마침표)만 남아도 된다.
PLACEHOLDER = re.compile(r"<[^<>\n]{1,200}>")
PLACEHOLDER_TAIL = re.compile(r"[`)\]}>.,!?:;'\"*_~]*")
# token 은 거의 늘 자격증명의 이름이다(access_token, root_token, telegram_token, SecurityToken). 앞 낱말이
# 개수·위치·문법을 말할 때만 아니다 — value_token·unit_token·next_token·max_token·page_token·csrf_token 같은
# JSON key 는 낱말 조각(수치·단위·구분자)을 가리킨다.
PLAIN_TOKEN_QUALIFIERS = frozenset({
    "bos", "cancel", "cancellation", "char", "cls", "completion", "continuation", "count", "csrf", "cursor",
    "delimiter", "end", "eof", "eol", "eos", "first", "input", "keyword", "last", "lexer", "literal", "mask",
    "max", "min", "newline", "next", "num", "output", "pad", "page", "parser", "prev", "previous", "prompt",
    "punctuation", "resume", "sep", "separator", "special", "start", "stop", "subword", "symbol", "sync",
    "total", "unit", "unk", "value", "whitespace", "word", "xsrf"})
# 값이 이런 낱말을 품은 이름이면(`token: value_token`, `password: DB_PASSWORD`) 비밀이 아니라 key 나 변수의 이름이다.
CREDENTIAL_WORDS = frozenset({"apikey", "credential", "credentials", "key", "passwd", "password", "secret",
                              "token"})
# 값 자리에 오지만 값이 아닌 것 — 타입 이름(`password: string`), 비었다·가렸다는 표시, 코드의 낱말.
NOT_A_VALUE = frozenset({
    "any", "await", "basic", "bearer", "bool", "boolean", "byte", "bytes", "char", "double", "false", "float",
    "hidden", "int", "integer", "jwt", "kerberos", "ldap", "masked", "new", "nil", "none", "null", "number",
    "oauth", "oauth2", "object", "oidc", "omitted", "optional", "redacted", "required", "saml", "str", "string",
    "tbd", "text", "todo", "true", "undefined", "unknown", "uuid", "varchar"})
# `${{ secrets.X }}`, `{{ password }}`, `$DB_PASSWORD`, `%PASSWORD%`, `{password}`, `get_token()`, `os.environ[…]`
# — 값을 가리키는 식이지 값이 아니다. `*****`·`xxxx`·`...` 는 가린 자리다.
REFERENCE = re.compile(r"\$\{|\{\{|\$\(|%\(|#\{|<%|%[A-Za-z_]|\$[A-Za-z_][A-Za-z0-9_]*$|\{[A-Za-z_][\w.]*\}$"
                       r"|[A-Za-z_][\w.]*[(\[]|[*•.…xX\-]+$")
HANGUL = re.compile(r"[가-힣]")
TOKEN_COUNT = re.compile(r"[0-9][0-9,.]*[kKmMbB%]?[)\]}.,]*")
CREDENTIAL_KEY = re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret|"
                            r"connection[_-]?string|private[_-]?key|client[_-]?secret|token)$")
# `block:<slug>:<16hex>:<n>` 의 꼬리. slug 가 password·access-token 처럼 자격증명 낱말로 끝나는 page 의
# block id 는 `password:5828…:1` 로 읽힌다 — 그 page 의 build 가 멈추고 훅이 id 를 가렸다.
BLOCK_ID_TAIL = re.compile(r"[0-9a-f]{16}:[1-9][0-9]*(?![0-9A-Za-z])")
IDENTIFIER_CHAR = re.compile(r"[A-Za-z0-9_.\-]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
SEGMENT_BREAK = re.compile(r"[_.\-\s]+|(?<=[a-z0-9])(?=[A-Z])")


def norm(value: Any) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def sha_bytes(data: bytes, length: int = 64) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def sha(text: str, length: int = 64) -> str:
    return sha_bytes(str(text).encode("utf-8"), length)


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def link_key(value: str) -> str:
    """Case, spacing, and a leading `page:` are the same page.

    Casefold first so `Page:Alpha Platform`, `page:alpha-platform`, and
    `alpha_platform` all collapse to `alpha-platform`.
    """
    text = norm(value).casefold()
    if text.startswith("page:"):
        text = text[5:]
    return LINK_SEPARATOR.sub("-", text).strip("-")


def mask_code_spans(text: str) -> str:
    """Blank inline code, keeping offsets, so code spans yield no wikilinks."""
    return INLINE_CODE.sub(lambda m: " " * len(m.group(0)), text)


def refs_in(text: str) -> list[str]:
    """`[[…]]` targets in `text`. An empty one (`[[ ]]`, `[[_]]`) names no page and is no link."""
    return sorted({norm(m.group(1)) for m in WIKILINK.finditer(mask_code_spans(text)) if link_key(m.group(1))})


def strip_ref_prefix(ref: str) -> tuple[str, str]:
    """`source:handbook` -> ('source', 'handbook'); a bare value keeps an empty prefix."""
    prefix, separator, rest = norm(ref).partition(":")
    if not separator or not norm(rest):
        return "", norm(ref)
    return prefix.lower(), norm(rest)


def reference_links(sources: Iterable[str] = (), supersedes: Iterable[str] = (),
                    related: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Frontmatter relations become graph edges, not just strings.

    `user:` and `raw:` name evidence outside the wiki and stay off the graph.
    """
    links: list[dict[str, Any]] = []
    for kind, refs in (("source", sources), ("supersedes", supersedes), ("related", related)):
        for ref in refs:
            prefix, target = strip_ref_prefix(ref)
            if prefix in {"user", "raw"} or not target:
                continue
            links.append({"target": target, "label": target, "kind": kind})
    return links


def implied_links(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Declared links + `[[wikilinks]]` in blocks + sources/supersedes/related.

    The viewer graph and the search diffusion graph both call this. A page that
    only cites `sources: [foo]` still gets a source edge; `[[Page:Alpha]]`
    resolves the same as `[[alpha]]`.
    """
    links = [dict(link) for link in page.get("links") or [] if norm(link.get("target"))]
    seen = {(link_key(link["target"]), link.get("kind", "wiki")) for link in links}

    def add(target: str, kind: str, block_id: str = "") -> None:
        key = (link_key(target), kind)
        if not key[0] or key in seen:
            return
        seen.add(key)
        link: dict[str, Any] = {"target": norm(target), "label": norm(target), "kind": kind}
        if block_id:
            link["block_id"] = block_id
        links.append(link)

    blocks = page.get("blocks") or {}
    for bid in page.get("block_order") or list(blocks):
        block = blocks.get(bid) or {}
        refs = block.get("refs")
        if refs is None:
            refs = refs_in(str(block.get("source_text", "")))
        for ref in refs:
            add(ref, "wiki", str(bid))
    for link in reference_links(page.get("sources") or (),
                                page.get("supersedes") or (),
                                page.get("related") or ()):
        add(link["target"], link["kind"])
    return links


def segments(name: str) -> list[str]:
    """`nextPageToken`, `NEXT_PAGE_TOKEN`, `next-page.token` -> ['next', 'page', 'token']."""
    return [part.lower() for part in SEGMENT_BREAK.split(name) if part]


def credential_name(identifier: str, word: str) -> bool:
    """Does `identifier`, which ends in the credential word `word`, name a credential?

    Every word but `token` does, whatever precedes it (db_password, JWT_SECRET,
    client_secret). So does `token`, bare, env-style (`GITHUB_TOKEN`) or after
    a qualifier (`refresh_token`, `root_token`) — unless the qualifier counts,
    places or parses (`value_token`, `next_token`, `max_token`, `csrf_token`).
    """
    if word.lower() != "token":
        return True
    name = identifier.strip("-\"'")
    if name.lower() == "token" or name.isupper():
        return True
    head = segments(name[:len(name) - len(word)])
    return not head or head[-1] not in PLAIN_TOKEN_QUALIFIERS


@functools.lru_cache(maxsize=4096)
def credential_key(key: str) -> bool:
    """A JSON object key whose value is a credential: `password`, `client_secret`, `root_token`."""
    match = CREDENTIAL_KEY.search(str(key))
    return bool(match) and credential_name(str(key), match.group(1))


def key_reference(value: str) -> bool:
    """`token: value_token`, `password: DB_PASSWORD`, `api_key: settings.API_KEY` — the value
    names a field or a variable: words only, ending in a credential word. `Password_2024`,
    `key-3ax6…`, `secret_9fJ2…` and `my.secret.pw` are values."""
    value = value.rstrip("`)]}>.,!?:;'\"*~")       # `token: value_token` 의 닫는 backtick 은 이름이 아니다
    if not IDENTIFIER.fullmatch(value):
        return False
    parts = segments(value)
    return len(parts) >= 2 and all(part.isalpha() for part in parts) and parts[-1] in CREDENTIAL_WORDS


def descriptive(value: str) -> bool:
    """Not a value at all: a type name, an empty or masked marker, an expression that
    points at the value, or Korean words describing the field (`password: 사용자 비밀번호`)."""
    word = value.strip("`)]}>.,!?:;'\"[(")
    return word.lower() in NOT_A_VALUE or bool(REFERENCE.match(value)) \
        or (bool(word) and len(HANGUL.findall(word)) * 2 >= len(word))


def token_like(value: str) -> bool:
    """A word after a bare `basic`/`bearer` that is a credential rather than English
    (`basic authentication`, `Basic JavaScript`, `a basic 2-column layout`): 20 characters
    or more, or 12 or more with a digit, `+`/`=` or base64-style mixed case."""
    if len(value) >= 20:
        return True
    mixed = bool(re.search(r"[a-z][A-Z]", value)) and bool(re.search(r"[A-Z][a-z]", value))
    return len(value) >= 12 and (bool(re.search(r"[0-9+=]", value)) or mixed)


def placeholder_covers(text: str, start: int, end: int) -> bool:
    """`<…>` begins the value at `start` and runs at least to its end."""
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    found = PLACEHOLDER.match(text, start)
    return bool(found) and (found.end() >= end or PLACEHOLDER_TAIL.fullmatch(text, found.end(), end) is not None)


def masked_at(text: str, start: int, end: int) -> bool:
    """The value is already the mask — and nothing more is glued to it (`(접속 정보 생략)hunter2` is not)."""
    while start < end and text[start].isspace():
        start += 1
    if not text.startswith(MASK, start):
        return False
    after = start + len(MASK)
    if text[after:end].strip():                       # 묶음(PEM 본문) 안에서 가림 뒤에 남은 것이 있다
        return False
    return after >= len(text) or not VALUE_CHAR.match(text[after])


def plain_value(text: str) -> bool:
    """A string that would hold a real credential — not empty, not masked, no placeholder,
    no field name, nothing merely descriptive."""
    value = text.strip()
    return bool(value) and not masked_at(value, 0, len(value)) and not placeholder_covers(value, 0, len(value)) \
        and not key_reference(value) and not descriptive(value) and not BLOCK_ID_TAIL.match(value)


def identifier_at(text: str, end: int, start: int) -> str:
    """The whole identifier that ends at `end` (its credential word starts at `start`)."""
    while start > 0 and IDENTIFIER_CHAR.match(text[start - 1]):
        start -= 1
    return text[start:end]


def exempt(match: re.Match[str], name: int, value: int, secret: int) -> bool:
    """Not a credential after all: already masked (redact stays idempotent), a `<…>`
    placeholder, a bare `basic`/`bearer` before plain English, a block id, a value that
    names a field or only describes one, or a name like `value_token`."""
    text = match.string
    if masked_at(text, match.start(secret), match.end(secret)):
        return True
    if placeholder_covers(text, match.start(secret), match.end(secret)):
        return True
    if match.re is AUTH_SCHEME and not token_like(match.group(value)):
        return True
    if not name:
        return False
    if not credential_name(identifier_at(text, match.end(name), match.start(name)), match.group(name)):
        return True
    if text.startswith(MASK, match.start(value)):     # 가림 뒤에 값이 붙었다 — `(접속 정보 생략)hunter2`
        return False
    found = match.group(value)
    if BLOCK_ID_TAIL.match(found) or key_reference(found) or descriptive(found):
        return True
    return match.group(name).lower() == "token" and bool(TOKEN_COUNT.fullmatch(found))   # `token: 128k` 는 개수다


def secret_matches(text: str, rule: tuple[re.Pattern[str], int, int, int]) -> Iterable[re.Match[str]]:
    """The credentials one rule finds in `text`. After an exempt match the scan resumes at its
    value, not past it — `page_token: password=hunter2` still finds the password."""
    pattern, name, value, secret = rule
    position = 0
    while True:
        match = pattern.search(text, position)
        if match is None:
            return
        if exempt(match, name, value, secret):
            position = max(match.start(value), match.start() + 1)
            continue
        yield match
        position = match.end()


def value_end(text: str, start: int, end: int) -> int:
    """Where a value that runs into masks ends — `ghp_…(접속 정보 생략)x` is one value, masked as one."""
    position = stop = start
    while True:
        if text.startswith(MASK, position):
            position += len(MASK)
            stop = position
            continue
        run = len(VALUE_RUN.match(text, position).group(0))
        found = text.find(MASK, position, position + run + len(MASK)) if run else -1
        if found != -1 and found < position + run:
            position = found
            continue
        position += run
        stop = position
        if not run or not text.startswith(MASK, position):
            return max(stop, end)


def redact_once(text: str) -> str:
    for rule in SECRET_RULES:
        pieces: list[str] = []
        last = 0
        for match in secret_matches(text, rule):
            start = match.start(rule[2])
            end = value_end(text, start, match.end(rule[2])) if rule[2] == rule[3] else match.end(rule[2])
            pieces += [text[last:start], MASK]
            last = end
        if pieces:
            text = "".join(pieces) + text[last:]
    return text


def redact(text: str) -> str:
    """Mask anything that looks like a credential. Strip control characters too.

    Idempotent: a masked value is left alone, and the rules run again until
    nothing changes — one rule masking part of a value another rule passed must
    not leave a tail the next call would mask. So the index, the renderer and
    the hook can each apply it without `(접속 정보 생략) 정보 생략)` piling up.
    """
    text = CONTROL.sub("", str(text or ""))
    if not SECRET_TRIGGER.search(text):
        return text
    for _ in range(8):                                # 회차마다 가리지 않은 글자가 줄어 곧 멈춘다
        masked = redact_once(text)
        if masked == text:
            break
        text = masked
    return text


def credential_items(key: Any, item: Any) -> Iterable[tuple[str, Any]]:
    """(sub-pointer, value) under a credential key that holds a credential — a plain string, a long
    number, or such strings in a list. Empty, masked, placeholder and descriptive values do not count."""
    if not isinstance(key, str) or not credential_key(key):
        return
    if isinstance(item, str) and plain_value(item):
        yield "", item
    elif isinstance(item, int) and not isinstance(item, bool) and len(str(abs(item))) >= 6:
        yield "", item
    elif isinstance(item, list):
        for index, element in enumerate(item):
            if isinstance(element, str) and plain_value(element):
                yield f"/{index}", element


def redact_value(value: Any) -> Any:
    """`redact` for a JSON value: strings are masked one by one and object keys are left
    alone, while a credential under a credential key (`"password": "x"`) is masked whole.

    Masking the serialised JSON instead could eat the backslash of an escaped
    quote and leave text that no longer parses.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            hits = {where for where, _found in credential_items(key, item)}
            if "" in hits:
                out[key] = MASK
            elif hits:
                out[key] = [MASK if f"/{index}" in hits else redact_value(element)
                            for index, element in enumerate(item)]
            else:
                out[key] = redact_value(item)
        return out
    return value


def secret_hit(text: str) -> str | None:
    """Short label if `text` looks like it holds a credential, else None.

    The label is the field name (`password`, `db_password`) or `credential` —
    never the secret value itself. Masked values and placeholders do not count.
    """
    text = CONTROL.sub("", str(text or ""))
    if not SECRET_TRIGGER.search(text):
        return None
    for rule in SECRET_RULES:
        for match in secret_matches(text, rule):
            name = rule[1]
            return identifier_at(text, match.end(name), match.start(name)).strip("-\"'") if name else "credential"
    return None


def pointer_token(key: Any) -> str:
    return str(key).replace("~", "~0").replace("/", "~1")


def secret_paths(value: Any, path: str = "") -> list[tuple[str, str]]:
    """(RFC 6901 pointer, label) for every string inside `value` that looks like a credential.

    A canonical page is copied verbatim to viewer/public/data, so a credential
    anywhere in it — body, summary, history, the ingested snapshot — leaks.
    """
    hits: list[tuple[str, str]] = []
    if isinstance(value, str):
        label = secret_hit(value)
        if label:
            hits.append((path or "/", label))
    elif isinstance(value, dict):
        for key, item in value.items():
            where = f"{path}/{pointer_token(key)}"
            # JSON key 는 이름만이지 값이 아니다 — `"value_token": "…"` 는 안 되고 `"password": "…"` 는 된다.
            if isinstance(key, str) and credential_key(key):
                hits.extend((where + sub, key) for sub, _found in credential_items(key, item))
            hits.extend(secret_paths(item, where))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            hits.extend(secret_paths(item, f"{path}/{index}"))
    return hits


def block_text(block: dict[str, Any]) -> str:
    """Search/display body: data.text/statement if present, else source_text."""
    data = block.get("data") or {}
    if isinstance(data, dict):
        for key in ("text", "statement"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key]
    text = block.get("source_text")
    return text if isinstance(text, str) else ""


def unresolved(block: dict[str, Any]) -> bool:
    resolution = block.get("resolution")
    status = resolution.get("status") if isinstance(resolution, dict) else None
    return block.get("kind") == "conflict" and status != "resolved"
