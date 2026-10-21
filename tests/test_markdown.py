"""Markdown -> block parsing and page construction."""
from __future__ import annotations

import random

from tests.support import FROZEN_DATE, WorkspaceCase, llmwiki

SAMPLE = """# 제목

첫 문단이다. [[sample-topic]]를 참조한다.

## 표

| 컬럼 | 값 |
| --- | --- |
| tier | 1 |

- 첫 항목
- 둘째 항목
  이어지는 줄

1. 하나
2. 둘

```python
print("hi")
```

> ⚠️ 상충: 문서마다 수치가 다르다.

> ✅ 현행: 최신 소스를 따른다.

> 그냥 인용이다.

---
"""


class ParseBlocksTest(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.blocks, self.order = llmwiki.parse_blocks("page:sample", SAMPLE)
        self.kinds = [self.blocks[b]["kind"] for b in self.order]

    def test_block_sequence(self) -> None:
        self.assertEqual(self.kinds, ["heading", "paragraph", "heading", "table", "list", "list",
                                      "code", "conflict", "current", "quote", "thematic_break"])

    def test_every_block_has_a_persistent_id_and_fingerprint(self) -> None:
        for bid in self.order:
            block = self.blocks[bid]
            self.assertEqual(block["id"], bid)
            self.assertTrue(bid.startswith("block:sample:"))
            self.assertEqual(block["fingerprint"],
                             llmwiki.block_fingerprint(block["kind"], block["source_text"]))

    def test_ids_are_not_positional(self) -> None:
        shifted, _ = llmwiki.parse_blocks("page:sample", "새 문단이 앞에 붙었다.\n\n" + SAMPLE)
        original = {self.blocks[b]["source_text"]: b for b in self.order}
        for bid, block in shifted.items():
            if block["source_text"] in original:
                self.assertEqual(bid, original[block["source_text"]])

    def test_repeated_text_gets_distinct_ids(self) -> None:
        blocks, order = llmwiki.parse_blocks("page:dup", "같은 문단\n\n같은 문단\n")
        self.assertEqual(len(order), 2)
        self.assertNotEqual(order[0], order[1])
        self.assertEqual(blocks[order[0]]["fingerprint"], blocks[order[1]]["fingerprint"])

    def test_table_rows_are_split(self) -> None:
        table = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "table")
        self.assertEqual(table["data"]["rows"][0], ["컬럼", "값"])
        self.assertEqual(table["data"]["rows"][2], ["tier", "1"])

    def test_an_escaped_pipe_stays_inside_its_cell(self) -> None:
        """GFM 처럼 `\\|` 는 칸을 나누지 않는다 — 타입(`str | None`)이나 셸 파이프를 칸에 적을 수 있다."""
        body = "| 칸 | 값 |\n| --- | --- |\n| 타입 | str \\| None |\n| 명령 | `a \\| b` |\n| 끝 | x \\| |\n"
        blocks, order = llmwiki.parse_blocks("page:x", body)
        rows = blocks[order[0]]["data"]["rows"]
        self.assertEqual(rows[2:], [["타입", "str | None"], ["명령", "`a | b`"], ["끝", "x |"]])
        self.assertEqual(llmwiki.parse_one("page:x", llmwiki.render_data("page:x", "table", blocks[order[0]]["data"]))["data"]["rows"], rows)

    def test_list_items_and_ordering(self) -> None:
        lists = [self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "list"]
        self.assertFalse(lists[0]["data"]["ordered"])
        self.assertEqual(lists[0]["data"]["items"], ["첫 항목", "둘째 항목 이어지는 줄"])
        self.assertTrue(lists[1]["data"]["ordered"])
        self.assertEqual(lists[1]["data"]["items"], ["하나", "둘"])

    def test_code_keeps_language_and_body(self) -> None:
        code = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "code")
        self.assertEqual(code["data"]["language"], "python")
        self.assertEqual(code["data"]["text"], 'print("hi")')

    def test_conflict_and_current_carry_resolution_status(self) -> None:
        by_kind = {self.blocks[b]["kind"]: self.blocks[b] for b in self.order}
        self.assertEqual(by_kind["conflict"]["resolution"]["status"], "unresolved")
        self.assertEqual(by_kind["current"]["resolution"]["status"], "resolved")
        self.assertEqual(by_kind["current"]["resolution"]["decided_at"], FROZEN_DATE)
        self.assertNotIn("resolution", by_kind["quote"])

    def test_wikilink_refs_are_collected(self) -> None:
        paragraph = next(self.blocks[b] for b in self.order if self.blocks[b]["kind"] == "paragraph")
        self.assertEqual(paragraph["refs"], ["sample-topic"])


class FrontmatterTest(WorkspaceCase):
    def test_parses_scalars_and_lists(self) -> None:
        meta, body = llmwiki.split_frontmatter(
            "---\ntype: concept\ntags: [a, b]\nprojects: []\ndraft: true\nnote: ~\n---\n# 본문\n")
        self.assertEqual(meta["type"], "concept")
        self.assertEqual(meta["tags"], ["a", "b"])
        self.assertEqual(meta["projects"], [])
        self.assertIs(meta["draft"], True)
        self.assertIsNone(meta["note"])
        self.assertEqual(body, "# 본문\n")

    def test_body_without_frontmatter_is_untouched(self) -> None:
        meta, body = llmwiki.split_frontmatter("# 본문\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "# 본문\n")


class PageFromMarkdownTest(WorkspaceCase):
    def test_builds_a_valid_page_with_snapshot_and_links(self) -> None:
        path = self.write_raw("note.md",
                              "---\ntype: concept\ntags: [tier]\n---\n# Sample Topic\n\n[[beta]] 참조.\n")
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(page["id"], "page:note")
        self.assertEqual(page["slug"], "note")
        self.assertEqual(page["title"], "Sample Topic")
        self.assertEqual(page["type"], "concept")
        self.assertEqual(page["tags"], ["tier"])
        self.assertEqual(page["created"], FROZEN_DATE)
        self.assertEqual(page["raw_ref"], "raw/note.md")
        self.assertEqual(page["source_snapshot"]["format"], "markdown")
        self.assertEqual(page["links"][0]["target"], "beta")
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])

    def test_cli_flags_override_frontmatter(self) -> None:
        path = self.write_raw("note.md", "---\ntype: source\n---\n# 제목\n")
        page = llmwiki.page_from_markdown(self.ws, path, "entity", ["beta"], summary="요약")
        self.assertEqual(page["type"], "entity")
        self.assertEqual(page["projects"], ["beta"])
        self.assertEqual(page["summary"], "요약")

    def test_title_falls_back_to_slug(self) -> None:
        path = self.write_raw("no-heading.md", "제목 없는 본문.\n")
        self.assertEqual(llmwiki.page_from_markdown(self.ws, path)["title"], "no-heading")

    def test_legacy_wiki_metadata_keeps_raw_evidence_and_normalizes_sources(self) -> None:
        path = self.write_raw(
            "legacy.md",
            "---\ntype: concept\nsources: [alpha, user:2026-08-19]\n"
            "raw: \"raw/beta/source.md\"\n---\n# Legacy\n\n[[alpha|표시명]] 첫 문단.\n",
        )
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(page["raw_ref"], "raw/beta/source.md")
        self.assertEqual(page["sources"], ["source:alpha", "user:2026-08-19"])
        self.assertEqual(page["summary"], "표시명 첫 문단.")
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])

    def test_summary_falls_back_to_first_list(self) -> None:
        path = self.write_raw("list-first.md", "# 목록\n\n- 첫 번째 핵심 사실\n- 다음 사실\n")
        self.assertEqual(llmwiki.page_from_markdown(self.ws, path)["summary"],
                         "첫 번째 핵심 사실 다음 사실")


class FrontmatterShapeTest(WorkspaceCase):
    """실제 노트가 쓰는 YAML 표기들 — 어느 쪽이든 같은 결과여야 한다."""

    def test_block_sequences_are_parsed(self) -> None:
        meta, body = llmwiki.split_frontmatter(
            "---\ntype: source\nprojects:\n  - OSE\n  - 공통\ntags:\n  - 주간보고\n"
            "  - 검색\n---\n# 본문\n")
        self.assertEqual(meta["projects"], ["OSE", "공통"])
        self.assertEqual(meta["tags"], ["주간보고", "검색"])
        self.assertEqual(body, "# 본문\n")

    def test_quotes_and_comments_are_stripped(self) -> None:
        meta, _ = llmwiki.split_frontmatter(
            '---\n# 메모다\ntags: ["a", \'b\', "c, d"]\nraw: "raw/x.md"\n---\n# 본문\n')
        self.assertEqual(meta["tags"], ["a", "b", "c, d"])
        self.assertEqual(meta["raw"], "raw/x.md")

    def test_empty_scalar_stays_null(self) -> None:
        meta, _ = llmwiki.split_frontmatter("---\nnote:\nsummary: ~\n---\n# 본문\n")
        self.assertIsNone(meta["note"])
        self.assertIsNone(meta["summary"])

    def test_string_list_coerces_and_deduplicates(self) -> None:
        self.assertEqual(llmwiki.string_list("alpha"), ["alpha"])
        self.assertEqual(llmwiki.string_list("alpha, beta"), ["alpha", "beta"])
        self.assertEqual(llmwiki.string_list(["a", " a ", "b"]), ["a", "b"])
        self.assertEqual(llmwiki.string_list(None), [])


class RelationLinkTest(WorkspaceCase):
    def test_block_sequence_metadata_reaches_the_page(self) -> None:
        path = self.write_raw(
            "weekly.md",
            "---\ntype: source\nprojects:\n  - OSE\ntags:\n  - 주간보고\nsources:\n"
            "  - user:2026-08-19\n  - page:alpha\n---\n# 주간보고\n\n[[alpha]] 진행.\n")
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(page["projects"], ["OSE"])
        self.assertEqual(page["tags"], ["주간보고"])
        self.assertEqual(page["sources"], ["user:2026-08-19", "page:alpha"])
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])

    def test_sources_supersedes_and_related_become_links(self) -> None:
        path = self.write_raw(
            "relations.md",
            "---\ntype: entity\nsources: [page:alpha, user:2026-08-19, raw:notes.md]\n"
            "supersedes: [old-page]\nrelated: [beta]\n---\n# 관계\n\n본문.\n")
        links = {(link["target"], link["kind"]) for link in llmwiki.page_from_markdown(self.ws, path)["links"]}
        self.assertIn(("alpha", "source"), links)
        self.assertIn(("old-page", "supersedes"), links)
        self.assertIn(("beta", "related"), links)
        # 위키 밖 근거는 그릴 노드가 없다.
        self.assertNotIn("notes.md", {target for target, _ in links})
        self.assertNotIn("2026-08-19", {target for target, _ in links})


class ParsedPagesAreConsistentTest(WorkspaceCase):
    """파서가 만든 page 는 validate 의 본문 일치 검사를 늘 통과한다 — ingest 가 자기 결과를 거절하지 않는다."""

    def test_an_unclosed_fence_at_the_end_with_blank_lines(self) -> None:
        path = self.write_raw("notes.md", "# Notes\n\n설명 문단.\n\n```bash\nmake build\n\n")
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual(llmwiki.consistency_errors(page), [])
        code = page["blocks"][page["block_order"][-1]]
        self.assertEqual((code["source_text"], code["data"]["text"]), ("```bash\nmake build", "make build"))
        self.assertEqual(llmwiki.ingest(self.ws, path)["page_id"], "page:notes")

    def test_random_markdown_always_parses_consistently(self) -> None:
        frags = ["# h", "## h2", "text", "```", "```py", "> q", "> ⚠️ 상충: c", "> ✅ 현행: d", "> ❓ 미결: e",
                 "owner: x", "- a", "1. b", "| a | b |", "| --- | --- |", "---", "", "  indented", "[[link]]",
                 "\x0c", " ", "\r", "\t"]
        rng = random.Random(20261020)
        for _ in range(3000):
            body = "\n".join(rng.choice(frags) for _ in range(rng.randint(1, 9))) + rng.choice(["", "\n", "\n\n"])
            blocks, order = llmwiki.parse_blocks("page:x", body)
            page = {"id": "page:x", "blocks": blocks, "block_order": order,
                    "links": llmwiki.page_links("page:x", blocks, order)}
            self.assertEqual(llmwiki.consistency_errors(page), [], repr(body))

    def test_a_wikilink_inside_code_is_no_link(self) -> None:
        body = "# Case\n\nrule `[['AUC-ROC','>=','0.85']]` and ``[[x]]`` but [[beta]]\n\n```\n[[gamma]]\n```\n"
        path = self.write_raw("case.md", body)
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual([link["target"] for link in page["links"]], ["beta"])
        self.assertEqual(page["blocks"][page["block_order"][1]]["refs"], ["beta"])
        self.assertEqual(page["blocks"][page["block_order"][2]]["refs"], [])
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])
        self.assertEqual(llmwiki.consistency_errors(page), [])

    def test_an_empty_wikilink_is_no_link(self) -> None:
        path = self.write_raw("case.md", "# Case\n\nx [[ ]] y [[_]] z [[beta]]\n")
        page = llmwiki.page_from_markdown(self.ws, path)
        self.assertEqual([link["target"] for link in page["links"]], ["beta"])
        self.assertEqual(page["blocks"][page["block_order"][1]]["refs"], ["beta"])
        self.assertEqual(llmwiki.validate_page(page, llmwiki.SchemaValidator.load(self.ws)), [])
        self.assertEqual(llmwiki.consistency_errors(page), [])
