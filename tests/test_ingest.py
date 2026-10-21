"""Ingest safety: raw immutability, secret refusal, overwrite policy, logging."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from tests.support import FROZEN_DATE, WorkspaceCase, llmwiki, make_page

NOTE = "---\ntype: concept\ntags: [tier]\n---\n# Sample Topic\n\n본문 [[beta]].\n"


class IngestTest(WorkspaceCase):
    def test_writes_a_sharded_canonical_page(self) -> None:
        path = self.write_raw("note.md", NOTE)
        result = llmwiki.ingest(self.ws, path)
        self.assertEqual(result["page_id"], "page:note")
        self.assertFalse(result["updated"])
        dest = self.root / result["dest"]
        self.assertTrue(dest.exists())
        self.assertEqual(dest.parent.name, "concepts")
        page = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(page["title"], "Sample Topic")
        self.assertEqual(page["raw_ref"], "raw/note.md")

    def test_raw_source_is_never_modified(self) -> None:
        path = self.write_raw("note.md", NOTE)
        before = path.read_bytes()
        llmwiki.ingest(self.ws, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in (self.root / "raw").iterdir()), ["note.md"])

    def test_appends_one_log_line(self) -> None:
        path = self.write_raw("note.md", NOTE)
        llmwiki.ingest(self.ws, path)
        rows = llmwiki.read_log(self.ws)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "ingest")
        self.assertEqual(rows[0]["page_id"], "page:note")
        self.assertEqual(rows[0]["source"], "raw/note.md")
        self.assertEqual(rows[0]["mode"], "create")
        self.assertTrue(rows[0]["at"].startswith(FROZEN_DATE))

    def test_log_is_append_only(self) -> None:
        llmwiki.ingest(self.ws, self.write_raw("a.md", "# A\n"))
        llmwiki.ingest(self.ws, self.write_raw("b.md", "# B\n"))
        rows = llmwiki.read_log(self.ws, limit=0)
        self.assertEqual([r["page_id"] for r in rows], ["page:a", "page:b"])

    def test_dry_run_writes_nothing(self) -> None:
        path = self.write_raw("note.md", NOTE)
        result = llmwiki.ingest(self.ws, path, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])
        self.assertEqual(llmwiki.read_log(self.ws), [])

    def test_refuses_to_overwrite_without_update(self) -> None:
        path = self.write_raw("note.md", NOTE)
        llmwiki.ingest(self.ws, path)
        with self.assertRaisesRegex(llmwiki.WikiError, "already exists"):
            llmwiki.ingest(self.ws, path)

    def test_update_preserves_created_and_appends_history(self) -> None:
        path = self.write_raw("note.md", NOTE)
        first = llmwiki.ingest(self.ws, path)
        original = json.loads((self.root / first["dest"]).read_text(encoding="utf-8"))
        original["created"] = "2020-01-01"
        (self.root / first["dest"]).write_text(json.dumps(original, ensure_ascii=False),
                                               encoding="utf-8")

        path.write_text(NOTE + "\n추가된 문단.\n", encoding="utf-8")
        result = llmwiki.ingest(self.ws, path, update=True)
        self.assertTrue(result["updated"])
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["created"], "2020-01-01")
        self.assertEqual(len(page["history"]), 2)
        self.assertIn("추가된 문단.", page["source_snapshot"]["text"])

    def test_rejects_credentials(self) -> None:
        for secret in ("password: hunter2", "API_KEY=sk-abc123", "access_token: ghp_x",
                       "connection_string=postgres://u:p@h/db",
                       "Authorization: Bearer abcdefghijklmnop",
                       "sk-abcdefghijklmnopqrstuvwxyz0123"):
            path = self.write_raw("leak.md", f"# 제목\n\n{secret}\n")
            with self.assertRaisesRegex(llmwiki.WikiError, "보안정보"):
                llmwiki.ingest(self.ws, path)
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])

    def test_allows_schema_field_names_without_values(self) -> None:
        path = self.write_raw("schema.md", "# 스키마\n\npassword 컬럼이 존재한다. (접속 정보 생략)\n")
        self.assertEqual(llmwiki.ingest(self.ws, path)["page_id"], "page:schema")

    def test_missing_source_is_reported(self) -> None:
        with self.assertRaisesRegex(llmwiki.WikiError, "source not found"):
            llmwiki.ingest(self.ws, self.root / "raw" / "nope.md")

    def test_refuses_to_reingest_from_the_canonical_store(self) -> None:
        self.write_pages([make_page("alpha", "# Alpha\n")], name="alpha.json")
        canonical = next((self.root / "wiki").rglob("*.json"))
        with self.assertRaisesRegex(llmwiki.WikiError, "canonical store"):
            llmwiki.ingest(self.ws, canonical)

    def test_json_source_is_validated_before_it_lands(self) -> None:
        bad = make_page("alpha", "# Alpha\n")
        bad["type"] = "nope"
        path = self.root / "raw" / "alpha.json"
        path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "invalid type"):
            llmwiki.ingest(self.ws, path)
        self.assertEqual(list((self.root / "wiki").rglob("*.json")), [])

    def test_json_source_is_accepted_and_stamped(self) -> None:
        path = self.root / "raw" / "alpha.json"
        path.write_text(json.dumps(make_page("alpha", "# Alpha\n"), ensure_ascii=False),
                        encoding="utf-8")
        result = llmwiki.ingest(self.ws, path, projects=["beta"], summary="요약")
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["projects"], ["beta"])
        self.assertEqual(page["summary"], "요약")
        self.assertEqual(page["history"][-1]["action"], "ingested")

    def test_json_arrays_are_rejected(self) -> None:
        path = self.root / "raw" / "many.json"
        path.write_text(json.dumps([make_page("alpha", "# Alpha\n")]), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "single page object"):
            llmwiki.ingest(self.ws, path)

    def test_shard_names_cannot_escape_the_store(self) -> None:
        self.assertEqual(llmwiki.safe_name("page:../../etc/passwd"), "etc-passwd.json")
        self.assertNotIn("/", llmwiki.safe_name("page:a/b"))
        self.assertTrue(llmwiki.safe_name("page:...").endswith(".json"))

    def test_ingested_page_survives_a_full_build(self) -> None:
        llmwiki.ingest(self.ws, self.write_raw("note.md", NOTE))
        llmwiki.ingest(self.ws, self.write_raw("beta.md", "# Beta\n\n[[note]]\n"))
        stats = llmwiki.build(self.ws)
        self.assertEqual(stats["pages"], 2)
        self.assertEqual(stats["edges"], 2)  # note -> beta and beta -> note


class LogCommandTest(WorkspaceCase):
    def test_append_and_read_back(self) -> None:
        llmwiki.append_log(self.ws, {"action": "query", "note": "tier 정의"})
        llmwiki.append_log(self.ws, {"action": "lint", "note": "0 errors"})
        rows = llmwiki.read_log(self.ws, limit=1)
        self.assertEqual(rows, [{"at": llmwiki.timestamp(), "action": "lint", "note": "0 errors"}])

    def test_empty_log_reads_as_empty_list(self) -> None:
        self.assertEqual(llmwiki.read_log(self.ws), [])

    def test_registers_a_new_project_group_during_ingest(self) -> None:
        note = "---\ntype: source\ntags: [신규, 분류]\nprojects: [newlab]\n---\n# 신규 연구\n"
        result = llmwiki.ingest(self.ws, self.write_raw("new.md", note))
        self.assertEqual(result["registered_groups"], ["newlab"])
        page = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["type"], "source")
        self.assertEqual(page["tags"], ["신규", "분류"])
        self.assertEqual(page["projects"], ["newlab"])
        groups = self.ws.load_groups()
        self.assertEqual(groups["project"]["newlab"]["match"], ["newlab"])
        self.assertEqual(llmwiki.project_group(page["projects"], groups), "newlab")


class ClassificationTest(WorkspaceCase):
    def test_changing_the_type_moves_the_page_to_its_new_directory(self) -> None:
        note = "---\ntype: source\n---\n# 노트\n\n본문.\n"
        first = llmwiki.ingest(self.ws, self.write_raw("note.md", note))
        self.assertEqual(first["dest"], "wiki/sources/note.json")

        second = llmwiki.ingest(self.ws, self.write_raw("note.md", note),
                                page_type="entity", update=True)
        self.assertEqual(second["dest"], "wiki/entities/note.json")
        self.assertEqual(second["moved_from"], "wiki/sources/note.json")
        self.assertFalse((self.root / "wiki" / "sources" / "note.json").exists())
        page = json.loads((self.root / second["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(page["type"], "entity")
        self.assertEqual(len(list((self.root / "wiki").rglob("note.json"))), 1)

    def test_refuses_to_overwrite_a_different_page_with_the_same_filename(self) -> None:
        other = make_page("other", "# 다른 페이지\n")
        target = self.root / "wiki" / "concepts" / "note.json"
        target.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(llmwiki.WikiError, "would overwrite a different page"):
            llmwiki.ingest(self.ws, self.write_raw("note.md", "---\ntype: concept\n---\n# 노트\n"),
                           update=True)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["id"], "page:other")

    def test_refuses_to_ingest_over_a_multi_page_file(self) -> None:
        self.write_pages([make_page("note", "# 노트\n"), make_page("other", "# 다른 노트\n")],
                         name="note.json")
        with self.assertRaisesRegex(llmwiki.WikiError, "several pages in one file"):
            llmwiki.ingest(self.ws, self.write_raw("note.md", "---\ntype: concept\n---\n# 노트\n"),
                           update=True)

    def test_json_page_metadata_is_normalised(self) -> None:
        page = make_page("json-note", "# JSON 노트\n\n본문.\n", type="entity")
        page["tags"] = ["a", " a ", "b"]
        page["projects"] = ["Sandbox Lab", "Sandbox Lab"]
        path = self.root / "raw" / "page.json"
        path.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        result = llmwiki.ingest(self.ws, path)
        stored = json.loads((self.root / result["dest"]).read_text(encoding="utf-8"))
        self.assertEqual(stored["tags"], ["a", "b"])
        self.assertEqual(stored["projects"], ["Sandbox Lab"])
        self.assertEqual(result["registered_groups"], ["sandbox-lab"])


class CliOnlyWorkflowTest(WorkspaceCase):
    """정본은 ingest·edit 로만 쓴다 — 그 둘만으로 page 의 머리와 본문을 다 바꿀 수 있어야 한다."""

    DRAFT = ("---\ntype: concept\ntags: [정책, \"쉼표, 든 태그\"]\nprojects: [beta]\n"
             "sources: [page:handbook]\nsupersedes: [old-policy]\nrelated: [beta-search]\n"
             "raw: raw/policy-notes.md\nsummary: \"[요약] 정책의 현행판 — 'quote' 포함\"\n---\n"
             "# Policy\n\n현행 정책 문단.\n\n대체 근거 문단.\n")

    def draft(self, text: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix="llmwiki-draft-")) / "policy.md"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(text, encoding="utf-8")
        return path

    def stored(self) -> dict:
        return llmwiki.find_page(self.ws, "policy")

    def test_a_draft_outside_the_repo_leaves_no_machine_path_behind(self) -> None:
        llmwiki.ingest(self.ws, self.draft("# Policy\n\n본문.\n"))
        page = self.stored()
        self.assertIsNone(page["raw_ref"])
        self.assertEqual(page["history"][-1]["note"], "policy.md")
        self.assertEqual(llmwiki.read_log(self.ws)[-1]["source"], "policy.md")

    def test_frontmatter_summary_and_raw_land_on_the_page(self) -> None:
        llmwiki.ingest(self.ws, self.draft(self.DRAFT))
        page = self.stored()
        self.assertEqual(page["summary"], "[요약] 정책의 현행판 — 'quote' 포함")
        self.assertEqual(page["raw_ref"], "raw/policy-notes.md")
        self.assertEqual(page["tags"], ["정책", "쉼표, 든 태그"])

    def test_render_then_ingest_update_round_trips_the_page_head(self) -> None:
        llmwiki.ingest(self.ws, self.draft(self.DRAFT))
        page = self.stored()
        anchor = page["block_order"][2]
        sup = next(l for l in page["links"] if l["kind"] == "supersedes")
        sup["block_id"] = anchor                                 # 옛 판이 anchor 를 달았다
        first = page["block_order"][1]
        page["block_aliases"] = {"block:policy:0000000000000000:1": first}
        (self.root / "wiki" / "concepts" / "policy.json").write_text(
            json.dumps(page, ensure_ascii=False), encoding="utf-8")
        rendered = llmwiki.render_markdown(self.stored())
        llmwiki.ingest(self.ws, self.draft(rendered), update=True)
        again = self.stored()
        for key in ("type", "tags", "projects", "sources", "raw_ref", "summary", "block_order", "blocks"):
            self.assertEqual(again[key], page[key], key)
        kinds = {(l["kind"], l["target"]): l.get("block_id") for l in again["links"]}
        self.assertEqual(kinds[("supersedes", "old-policy")], anchor)
        self.assertIn(("related", "beta-search"), kinds)
        self.assertEqual(again["block_aliases"], {"block:policy:0000000000000000:1": first})

    def test_a_block_is_appended_and_the_head_changed_without_touching_json(self) -> None:
        llmwiki.ingest(self.ws, self.draft(self.DRAFT))
        last = self.stored()["block_order"][-1]
        llmwiki.edit(self.ws, "policy", last, "대체 근거 문단.\n\n새 소스가 더한 문단 [[beta-search]].")
        rendered = llmwiki.render_markdown(self.stored()).replace("tags: [", "tags: [새태그, ", 1)
        llmwiki.ingest(self.ws, self.draft(rendered), update=True)
        page = self.stored()
        self.assertEqual(page["tags"][0], "새태그")
        self.assertIn("새 소스가 더한 문단 [[beta-search]].",
                      [b["source_text"] for b in page["blocks"].values()])
        self.assertEqual(llmwiki.consistency_errors(page), [])


class UpdateContinuityTest(WorkspaceCase):
    """`ingest --update` 는 바뀌지 않은 block 의 id·판정·본문 밖 data 를 잇는다."""

    def draft(self, text: str, slug: str) -> Path:
        path = Path(tempfile.mkdtemp(prefix="llmwiki-draft-")) / f"{slug}.md"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_repeated_content_keeps_its_ids_after_an_edit_and_a_round_trip(self) -> None:
        llmwiki.ingest(self.ws, self.write_raw("dup.md", "# Dup\n\n같은 문단.\n\n같은 문단.\n"))
        first, second = llmwiki.find_page(self.ws, "dup")["block_order"][1:3]
        edited = llmwiki.edit(self.ws, "dup", first, "바뀐 문단.")["block_id"]
        llmwiki.ingest(self.ws, self.draft(llmwiki.render_markdown(llmwiki.find_page(self.ws, "dup")), "dup"),
                       update=True)
        page = llmwiki.find_page(self.ws, "dup")
        self.assertEqual(page["block_order"][1:3], [edited, second])
        self.assertEqual(llmwiki.resolve(self.ws, second)[1]["source_text"], "같은 문단.")
        self.assertEqual(llmwiki.resolve(self.ws, first)[1]["source_text"], "바뀐 문단.")
        self.assertEqual(llmwiki.consistency_errors(page), [])

    def test_judgements_and_claims_survive_a_head_change(self) -> None:
        dest = self.root / llmwiki.ingest(self.ws, self.write_raw(
            "pol.md", "# Pol\n\n> ⚠️ 상충: A 는 3, B 는 5.\n\n> ✅ 현행: 5 를 따른다.\n"))["dest"]
        page = json.loads(dest.read_text(encoding="utf-8"))
        conflict, current = page["block_order"][1:3]
        page["blocks"][conflict]["resolution"] = {"status": "resolved", "current": "B", "decided_at": "2026-08-01"}
        page["blocks"][conflict]["data"]["claims"] = ["A 기준", "B 기준"]
        page["blocks"][current]["resolution"]["decided_at"] = "2026-08-01"
        dest.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        rendered = llmwiki.render_markdown(page).replace("tags: []", "tags: [새태그]")
        llmwiki.ingest(self.ws, self.draft(rendered, "pol"), update=True)
        again = llmwiki.find_page(self.ws, "pol")
        self.assertEqual(again["tags"], ["새태그"])
        self.assertEqual(again["blocks"][conflict]["resolution"],
                         {"status": "resolved", "current": "B", "decided_at": "2026-08-01"})
        self.assertEqual(again["blocks"][conflict]["data"]["claims"], ["A 기준", "B 기준"])
        self.assertEqual(again["blocks"][current]["resolution"]["decided_at"], "2026-08-01")
        self.assertEqual(llmwiki.lint(self.ws)[0], [])

    def test_frontmatter_summary_is_read_as_written(self) -> None:
        cases = {"summary: [초안] 배포 정책 [v2]\n": "[초안] 배포 정책 [v2]",
                 "summary: >\n  두 줄로\n  쓴 요약\n": "두 줄로 쓴 요약",
                 "summary: |\n  첫 줄\n  둘째 줄\ntags: [t]\n": "첫 줄 둘째 줄"}
        for number, (head, expected) in enumerate(cases.items()):
            path = self.write_raw(f"s{number}.md", f"---\ntype: concept\n{head}---\n# S\n\n본문.\n")
            self.assertEqual(llmwiki.page_from_markdown(self.ws, path)["summary"], expected, head)


class NonMarkdownIngestTest(WorkspaceCase):
    def test_html_becomes_a_page_of_its_text(self) -> None:
        path = self.write_raw("spec.html", "<html><body><h1>Spec</h1><p>first rule</p><script>x()</script></body></html>")
        result = llmwiki.ingest(self.ws, path)
        page = self.read_json(result["dest"])
        self.assertEqual(page["raw_ref"], "raw/spec.html")
        self.assertIn("first rule", page["source_snapshot"]["text"])
        self.assertNotIn("x()", page["source_snapshot"]["text"])
        self.assertEqual(page["source_snapshot"]["format"], "markdown")

    def test_a_binary_without_text_becomes_a_stub(self) -> None:
        path = self.root / "raw" / "shot.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        page = self.read_json(llmwiki.ingest(self.ws, path)["dest"])
        self.assertEqual(page["source_snapshot"]["format"], "text")
        self.assertIn("raw/shot.png", page["source_snapshot"]["text"])

    def test_a_credential_in_a_converted_body_is_masked(self) -> None:
        path = self.write_raw("conf.csv", "name,value\ndb,password=hunter2secret\n")
        page = self.read_json(llmwiki.ingest(self.ws, path)["dest"])
        self.assertNotIn("hunter2secret", json.dumps(page, ensure_ascii=False))

    def test_slug_and_tag_override(self) -> None:
        path = self.write_raw("a/README.md", "# A\n")
        page = self.read_json(llmwiki.ingest(self.ws, path, slug="a-readme", tags=["note"])["dest"])
        self.assertEqual(page["slug"], "a-readme")
        self.assertIn("note", page["tags"])

    def test_a_plain_json_file_is_content_not_a_page(self) -> None:
        path = self.write_raw("settings.json", '{"theme": "dark"}')
        page = self.read_json(llmwiki.ingest(self.ws, path)["dest"])
        self.assertIn("dark", page["source_snapshot"]["text"])
