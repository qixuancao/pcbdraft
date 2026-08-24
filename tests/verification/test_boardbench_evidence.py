from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pcbdraft.core.errors import ValidationError
from pcbdraft.core.io import atomic_write_json, atomic_write_text
from pcbdraft.verification.boardbench import (
    BoardBenchCorrection,
    BoardBenchHardware,
    BoardBenchReview,
    BoardBenchRun,
    BoardBenchSelection,
    FailureClassification,
    ModificationDecision,
    OrderabilityEvidence,
    RailMeasurement,
    SelectionEntry,
    StructuralDiffEntry,
    artifact_sha256,
    build_inventory,
    case_sha256,
    load_campaign,
    load_review,
    review_checklist_for_case,
    store_run,
    write_artifact,
)
from pcbdraft.verification.boardbench_evidence import (
    create_review_templates,
    import_hardware,
    import_review,
    import_selection,
    link_hardware_external_evidence,
    link_review_external_evidence,
)
from pcbdraft.verification.boardbench_runner import (
    create_campaign,
    initialize_run_receipts,
)
from pcbdraft.verification.boardbench_v2 import store_run_v2, terminal_run_v2
from tests.verification.test_boardbench_runner import NOW, _corpus, _environment

HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


class BoardBenchEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.corpus = _corpus()
        self.campaign_root = create_campaign(
            self.root / "campaigns",
            self.corpus,
            campaign_id="campaign-v1",
            evaluator_version="boardbench-evaluator-v1",
            environment=_environment(),
            created_at=NOW,
            wall_timeout_seconds=30.0,
        )
        self.campaign = load_campaign(self.campaign_root / "campaign.json")
        planned = initialize_run_receipts(
            self.campaign_root, self.campaign, self.corpus
        )
        self.runs: dict[str, BoardBenchRun] = {}
        for index, item in enumerate(planned):
            run_root = self.campaign_root / "runs" / item.run_id
            path = run_root / "run.json"
            running = replace(item, status="running", started_at=NOW)
            store_run(path, running)
            artifacts = run_root / "artifacts"
            artifacts.mkdir()
            atomic_write_text(artifacts / "execution.txt", "fixture\n")
            if index < len(planned) - 1:
                atomic_write_text(
                    artifacts
                    / "repository"
                    / "projects"
                    / item.run_id
                    / "design.kicad_sch",
                    "(kicad_sch (version 20250114) (generator pcbdraft))\n",
                )
            terminal_v2 = terminal_run_v2(
                running,
                self.campaign,
                artifacts,
                completed_at=NOW,
                fallback_status="completed",
                fallback_reason="fixture",
                final_response="done",
                duration_seconds=0.0,
                worker_exit_code=0,
                inventory=build_inventory(artifacts),
            )
            store_run_v2(path, terminal_v2)
            terminal = terminal_v2.to_legacy()
            self.runs[item.run_id] = terminal

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _completed_review(self, run_id: str) -> BoardBenchReview:
        plan = next(plan for plan in self.campaign.runs if plan.run_id == run_id)
        case = next(case for case in self.corpus.cases if case.id == plan.case_id)
        checklist = tuple(
            replace(
                item,
                disposition="pass",
                evidence_note="Inspected this source-authored review obligation.",
            )
            for item in review_checklist_for_case(case)
        )
        return BoardBenchReview(
            campaign_id=self.campaign.campaign_id,
            run_id=run_id,
            source_campaign_sha256=artifact_sha256(self.campaign),
            source_corpus_sha256=artifact_sha256(self.corpus),
            source_case_sha256=case_sha256(case),
            source_run_sha256=artifact_sha256(self.runs[run_id]),
            source_score_sha256=None,
            reviewer="Engineer One",
            reviewed_at=NOW,
            outcome="pass_without_schematic_change",
            functional_correctness="pass",
            orderable_state="pass",
            orderability_evidence=(
                OrderabilityEvidence(
                    slot_ids=("device",),
                    manufacturer_part_number="FIXTURE-DEVICE",
                    status="orderable",
                    as_of=NOW,
                    source_kind="manufacturer",
                    source_name="Fixture manufacturer",
                    source_url="https://example.com/parts/fixture-device",
                    note="Reviewer inspected a dated fixture source.",
                ),
            ),
            active_engineer_minutes=12.0,
            not_applicable_reason=None,
            checklist=checklist,
            findings=(),
            modifications=(),
            final_failure=None,
        )

    def _write_correction(
        self, run_id: str, candidate_hash: str
    ) -> BoardBenchCorrection:
        review = replace(
            self._completed_review(run_id),
            outcome="pass_after_changes",
            modifications=(
                ModificationDecision(
                    id="decision-1",
                    change_type="functional",
                    description="Repair the generated circuit.",
                    finding_ids=(),
                ),
            ),
            final_failure=FailureClassification(
                stage="circuit_design",
                causes=("model_reasoning",),
                owners=("model",),
                reason="The generated circuit needed one correction.",
            ),
        )
        write_artifact(self.campaign_root / "reviews" / run_id / "review.json", review)
        correction = BoardBenchCorrection(
            campaign_id=self.campaign.campaign_id,
            run_id=run_id,
            source_run_sha256=artifact_sha256(self.runs[run_id]),
            source_review_sha256=artifact_sha256(review),
            created_at=NOW,
            generated_snapshot_sha256=HASH_A,
            corrected_snapshot_sha256=HASH_B,
            manufacturing_candidate_sha256=candidate_hash,
            decision_ids=("decision-1",),
            changes=(
                StructuralDiffEntry(
                    area="components",
                    operation="changed",
                    identity="U1",
                    before_sha256=HASH_A,
                    after_sha256=HASH_B,
                    description="Fixture correction.",
                ),
            ),
            inventory=(),
        )
        write_artifact(
            self.campaign_root / "corrections" / run_id / "correction.json",
            correction,
        )
        return correction

    def _not_applicable_review(self, run_id: str) -> BoardBenchReview:
        review = self._completed_review(run_id)
        return replace(
            review,
            outcome="not_applicable",
            functional_correctness="not_applicable",
            orderable_state="not_applicable",
            orderability_evidence=(),
            not_applicable_reason="No inspectable schematic was retained.",
            checklist=tuple(
                replace(
                    item,
                    disposition="not_applicable",
                    evidence_note="No retained schematic exists for this item.",
                )
                for item in review.checklist
            ),
            final_failure=FailureClassification(
                stage="kicad_materialization",
                causes=("environment_infrastructure",),
                owners=("environment",),
                reason="The terminal run retained no inspectable schematic.",
            ),
        )

    def _selection(self) -> tuple[BoardBenchSelection, dict[str, BoardBenchCorrection]]:
        case_by_id = {case.id: case for case in self.corpus.cases}
        chosen = []
        corrections: dict[str, BoardBenchCorrection] = {}
        seen: set[str] = set()
        for plan in self.campaign.runs:
            category = case_by_id[plan.case_id].category
            if category in seen:
                continue
            seen.add(category)
            candidate_hash = f"{len(seen):064x}"
            corrections[plan.run_id] = self._write_correction(
                plan.run_id, candidate_hash
            )
            chosen.append(
                SelectionEntry(
                    category=category,
                    run_id=plan.run_id,
                    source_artifact_sha256=candidate_hash,
                    rationale="Representative fixture.",
                )
            )
        return (
            BoardBenchSelection(
                campaign_id=self.campaign.campaign_id,
                created_at=NOW,
                selector="Engineer One",
                selections=tuple(chosen),
            ),
            corrections,
        )

    def test_review_templates_bind_all_sixty_terminal_runs(self) -> None:
        paths = create_review_templates(self.campaign_root, self.corpus)
        self.assertEqual(len(paths), 60)
        first = load_review(paths[0])
        plan = next(item for item in self.campaign.runs if item.run_id == first.run_id)
        case = next(item for item in self.corpus.cases if item.id == plan.case_id)
        self.assertEqual(first.outcome, "not_reviewed")
        self.assertEqual(
            first.source_run_sha256, artifact_sha256(self.runs[first.run_id])
        )
        self.assertEqual(first.checklist, review_checklist_for_case(case))
        self.assertEqual(
            tuple(item.id for item in first.checklist),
            tuple(item.id for item in review_checklist_for_case(case)),
        )
        self.assertTrue(
            all(len(item.id.rsplit("-", 1)[-1]) == 64 for item in first.checklist)
        )
        other_case = next(item for item in self.corpus.cases if item.id != case.id)
        self.assertNotEqual(
            tuple(item.id for item in first.checklist),
            tuple(item.id for item in review_checklist_for_case(other_case)),
        )
        with self.assertRaisesRegex(ValidationError, "already exists"):
            create_review_templates(self.campaign_root, self.corpus)

    def test_review_templates_reject_corpus_source_hash_drift(self) -> None:
        changed_corpus = replace(
            self.corpus,
            methodology=self.corpus.methodology + " Changed after campaign creation.",
        )
        with self.assertRaisesRegex(ValidationError, "corpus does not match"):
            create_review_templates(self.campaign_root, changed_corpus)
        self.assertFalse((self.campaign_root / "review-templates").exists())

    def test_review_template_failure_leaves_no_partial_output(self) -> None:
        calls = 0

        def fail_during_staging(path: str | Path, artifact: BoardBenchReview) -> Path:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ValidationError("forced template failure")
            return write_artifact(path, artifact)

        with (
            mock.patch(
                "pcbdraft.verification.boardbench_evidence.write_artifact",
                side_effect=fail_during_staging,
            ),
            self.assertRaisesRegex(ValidationError, "forced template failure"),
        ):
            create_review_templates(self.campaign_root, self.corpus)
        self.assertFalse((self.campaign_root / "review-templates").exists())
        self.assertFalse(
            any(
                path.name.startswith(".review-templates-")
                for path in self.campaign_root.iterdir()
            )
        )

    def test_review_import_rejects_stale_source_and_is_write_once(self) -> None:
        run_id = self.campaign.runs[0].run_id
        submission = self.root / "review.json"
        write_artifact(
            submission,
            replace(self._completed_review(run_id), source_run_sha256=HASH_C),
        )
        with self.assertRaisesRegex(ValidationError, "source hash or identity"):
            import_review(self.campaign_root, self.corpus, submission)

        valid = self.root / "valid-review.json"
        write_artifact(valid, self._completed_review(run_id))
        target = import_review(self.campaign_root, self.corpus, valid)
        self.assertEqual(load_review(target), self._completed_review(run_id))
        with self.assertRaisesRegex(ValidationError, "already exists"):
            import_review(self.campaign_root, self.corpus, valid)

    def test_review_import_rejects_foreign_or_changed_case_items(self) -> None:
        run_id = self.campaign.runs[0].run_id
        original = self._completed_review(run_id)
        variants = (
            (
                "foreign item",
                replace(
                    original,
                    checklist=(
                        replace(original.checklist[0], id="foreign-review-item"),
                        *original.checklist[1:],
                    ),
                ),
                "item set differs",
            ),
            (
                "changed source text",
                replace(
                    original,
                    checklist=(
                        replace(
                            original.checklist[0],
                            requirement="Reviewer-authored replacement requirement.",
                        ),
                        *original.checklist[1:],
                    ),
                ),
                "item differs from source case",
            ),
        )
        for index, (label, review, error) in enumerate(variants):
            submission = self.root / f"invalid-review-{index}.json"
            write_artifact(submission, review)
            with (
                self.subTest(label=label),
                self.assertRaisesRegex(ValidationError, error),
            ):
                import_review(self.campaign_root, self.corpus, submission)

    def test_review_import_rejects_corpus_source_hash_drift(self) -> None:
        run_id = self.campaign.runs[0].run_id
        submission = self.root / "review.json"
        write_artifact(submission, self._completed_review(run_id))
        changed_corpus = replace(
            self.corpus,
            methodology=self.corpus.methodology + " Changed after campaign creation.",
        )
        with self.assertRaisesRegex(ValidationError, "corpus does not match"):
            import_review(self.campaign_root, changed_corpus, submission)

    def test_review_import_rejects_all_portable_source_hash_drift(self) -> None:
        run_id = self.campaign.runs[0].run_id
        original = self._completed_review(run_id)
        for field in (
            "source_campaign_sha256",
            "source_corpus_sha256",
            "source_case_sha256",
            "source_run_sha256",
        ):
            with self.subTest(field=field):
                submission = self.root / f"drift-{field}.json"
                write_artifact(submission, replace(original, **{field: HASH_C}))
                with self.assertRaisesRegex(ValidationError, "source hash or identity"):
                    import_review(self.campaign_root, self.corpus, submission)

    def test_review_import_derives_whole_review_applicability_from_inventory(
        self,
    ) -> None:
        inspectable_run_id = self.campaign.runs[0].run_id
        invalid_na = self.root / "invalid-na.json"
        write_artifact(
            invalid_na,
            self._not_applicable_review(inspectable_run_id),
        )
        with self.assertRaisesRegex(ValidationError, "has an inspectable schematic"):
            import_review(self.campaign_root, self.corpus, invalid_na)

        no_schematic_run_id = self.campaign.runs[-1].run_id
        invalid_applicable = self.root / "invalid-applicable.json"
        write_artifact(
            invalid_applicable,
            self._completed_review(no_schematic_run_id),
        )
        with self.assertRaisesRegex(ValidationError, "no inspectable schematic"):
            import_review(self.campaign_root, self.corpus, invalid_applicable)

        valid_na = self.root / "valid-na.json"
        write_artifact(valid_na, self._not_applicable_review(no_schematic_run_id))
        imported = import_review(self.campaign_root, self.corpus, valid_na)
        self.assertEqual("not_applicable", load_review(imported).outcome)

    def test_selection_requires_category_and_manufacturing_candidate_hashes(
        self,
    ) -> None:
        selection, _corrections = self._selection()
        submission = self.root / "selection.json"
        write_artifact(submission, selection)
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.verify_correction_bundle",
            side_effect=lambda root, **_kwargs: _corrections[Path(root).name],
        ):
            target = import_selection(self.campaign_root, self.corpus, submission)
        self.assertEqual(target, self.campaign_root / "selection.json")

        changed = replace(
            selection,
            campaign_id="campaign-other",
        )
        wrong = self.root / "wrong-selection.json"
        write_artifact(wrong, changed)
        with self.assertRaisesRegex(ValidationError, "campaign id"):
            import_selection(self.campaign_root, self.corpus, wrong)

    def test_selection_accepts_reviewed_no_change_run_without_fake_correction(
        self,
    ) -> None:
        case_by_id = {case.id: case for case in self.corpus.cases}
        entries = []
        seen: set[str] = set()
        for plan in self.campaign.runs:
            category = case_by_id[plan.case_id].category
            if category in seen:
                continue
            seen.add(category)
            review = self._completed_review(plan.run_id)
            write_artifact(
                self.campaign_root / "reviews" / plan.run_id / "review.json",
                review,
            )
            entries.append(
                SelectionEntry(
                    category=category,
                    run_id=plan.run_id,
                    source_artifact_sha256=artifact_sha256(self.runs[plan.run_id]),
                    rationale="No schematic correction was required.",
                )
            )
        selection = BoardBenchSelection(
            campaign_id=self.campaign.campaign_id,
            created_at=NOW,
            selector="Engineer One",
            selections=tuple(entries),
        )
        submission = self.root / "no-change-selection.json"
        write_artifact(submission, selection)
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.verify_correction_bundle"
        ) as verifier:
            import_selection(self.campaign_root, self.corpus, submission)
        verifier.assert_not_called()

    def test_selection_rechecks_review_v3_sources_instead_of_trusting_outcome(
        self,
    ) -> None:
        case_by_id = {case.id: case for case in self.corpus.cases}
        entries = []
        seen: set[str] = set()
        corrupted_run_id = ""
        for plan in self.campaign.runs:
            category = case_by_id[plan.case_id].category
            if category in seen:
                continue
            seen.add(category)
            review = self._completed_review(plan.run_id)
            if not corrupted_run_id:
                corrupted_run_id = plan.run_id
                review = replace(review, source_case_sha256=HASH_C)
            review_path = self.campaign_root / "reviews" / plan.run_id / "review.json"
            atomic_write_json(review_path, review.to_dict())
            entries.append(
                SelectionEntry(
                    category=category,
                    run_id=plan.run_id,
                    source_artifact_sha256=artifact_sha256(self.runs[plan.run_id]),
                    rationale="Adversarial source-gate fixture.",
                )
            )
        selection = BoardBenchSelection(
            campaign_id=self.campaign.campaign_id,
            created_at=NOW,
            selector="Engineer One",
            selections=tuple(entries),
        )
        submission = self.root / "source-bypass-selection.json"
        write_artifact(submission, selection)
        with self.assertRaisesRegex(ValidationError, "source hash or identity"):
            import_selection(self.campaign_root, self.corpus, submission)

    def test_hardware_import_copies_and_verifies_real_attachments(self) -> None:
        selection, corrections = self._selection()
        selection_path = self.root / "selection.json"
        write_artifact(selection_path, selection)
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.verify_correction_bundle",
            side_effect=lambda root, **_kwargs: corrections[Path(root).name],
        ):
            import_selection(self.campaign_root, self.corpus, selection_path)
        selected = selection.selections[0]
        correction = corrections[selected.run_id]

        submission = self.root / "hardware-submission"
        attachments = submission / "attachments"
        attachments.mkdir(parents=True)
        atomic_write_text(attachments / "power-on.txt", "measured at current limit\n")
        hardware = BoardBenchHardware(
            campaign_id=self.campaign.campaign_id,
            run_id=selected.run_id,
            source_kind="correction",
            source_artifact_sha256=artifact_sha256(correction),
            category=selected.category,
            board_revision="A1",
            board_serial="fixture-001",
            revision_count=1,
            fabricator="Fixture Fab",
            operator="Engineer One",
            observed_at=NOW,
            fabricator_accepted="pass",
            solderability="pass",
            first_power_no_short="pass",
            firmware_download="pass",
            core_function="pass",
            rails=(RailMeasurement("3V3", "V", 3.2, 3.4, 3.3, "pass"),),
            notes="Fixture evidence only.",
            attachments=build_inventory(attachments),
        )
        write_artifact(submission / "hardware.json", hardware)
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.verify_correction_bundle",
            return_value=correction,
        ):
            imported = import_hardware(self.campaign_root, submission)
        self.assertEqual(imported.hardware, hardware)
        self.assertEqual(
            build_inventory(imported.root / "attachments"), hardware.attachments
        )
        derived = self.root / "hardware-linked-project"
        derived.mkdir()
        with (
            mock.patch(
                "pcbdraft.verification.boardbench_evidence.record_external_evidence",
                return_value=derived / "external-evidence.json",
            ) as record,
            mock.patch(
                "pcbdraft.verification.boardbench_evidence.verify_correction_bundle",
                return_value=correction,
            ),
        ):
            result = link_hardware_external_evidence(
                self.campaign_root,
                derived,
                imported.root,
                test_plan="Current-limited bring-up and functional fixture test.",
            )
        self.assertEqual(result, derived / "external-evidence.json")
        self.assertEqual(record.call_args.kwargs["level"], "L7")
        self.assertEqual(record.call_args.kwargs["outcome"], "pass")
        with (
            self.assertRaisesRegex(ValidationError, "already exists"),
            mock.patch(
                "pcbdraft.verification.boardbench_evidence.verify_correction_bundle",
                return_value=correction,
            ),
        ):
            import_hardware(self.campaign_root, submission)

    def test_release_hardware_retains_and_rehashes_its_source_artifact(self) -> None:
        case_by_id = {case.id: case for case in self.corpus.cases}
        entries = []
        seen: set[str] = set()
        for plan in self.campaign.runs:
            category = case_by_id[plan.case_id].category
            if category in seen:
                continue
            seen.add(category)
            review = self._completed_review(plan.run_id)
            write_artifact(
                self.campaign_root / "reviews" / plan.run_id / "review.json",
                review,
            )
            entries.append(
                SelectionEntry(
                    category=category,
                    run_id=plan.run_id,
                    source_artifact_sha256=artifact_sha256(self.runs[plan.run_id]),
                    rationale="Release-backed representative fixture.",
                )
            )
        selection = BoardBenchSelection(
            campaign_id=self.campaign.campaign_id,
            created_at=NOW,
            selector="Engineer One",
            selections=tuple(entries),
        )
        selection_submission = self.root / "release-selection.json"
        write_artifact(selection_submission, selection)
        import_selection(self.campaign_root, self.corpus, selection_submission)

        selected = selection.selections[0]
        release = self.root / "manufacturing-release.zip"
        atomic_write_text(release, "bounded manufacturing release fixture\n")
        release_hash = hashlib.sha256(release.read_bytes()).hexdigest()
        submission = self.root / "release-hardware"
        attachments = submission / "attachments"
        attachments.mkdir(parents=True)
        atomic_write_text(attachments / "bring-up.txt", "current-limited pass\n")
        hardware = BoardBenchHardware(
            campaign_id=self.campaign.campaign_id,
            run_id=selected.run_id,
            source_kind="release",
            source_artifact_sha256=release_hash,
            category=selected.category,
            board_revision="A1",
            board_serial="release-fixture-001",
            revision_count=1,
            fabricator="Fixture Fab",
            operator="Engineer One",
            observed_at=NOW,
            fabricator_accepted="pass",
            solderability="pass",
            first_power_no_short="pass",
            firmware_download="pass",
            core_function="pass",
            rails=(RailMeasurement("3V3", "V", 3.2, 3.4, 3.3, "pass"),),
            notes="Fixture evidence only.",
            attachments=build_inventory(attachments),
        )
        write_artifact(submission / "hardware.json", hardware)
        imported = import_hardware(
            self.campaign_root, submission, release_artifact=release
        )
        retained = imported.root / "source" / "release-artifact"
        self.assertEqual(retained.read_bytes(), release.read_bytes())
        self.assertEqual(
            hashlib.sha256(retained.read_bytes()).hexdigest(), release_hash
        )

        linked = self.root / "release-linked-project"
        linked.mkdir()
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.record_external_evidence",
            return_value=linked / "external-evidence.json",
        ) as record:
            link_hardware_external_evidence(
                self.campaign_root,
                linked,
                imported.root,
                test_plan="Current-limited bring-up.",
            )
        self.assertIn(str(retained), record.call_args.kwargs["artifacts"])

        retained.write_bytes(b"tampered")
        derived = self.root / "tampered-release-link"
        derived.mkdir()
        with self.assertRaisesRegex(ValidationError, "release source hash"):
            link_hardware_external_evidence(
                self.campaign_root,
                derived,
                imported.root,
                test_plan="Current-limited bring-up.",
            )

    def test_l6_linkage_refuses_to_mutate_raw_run_repository(self) -> None:
        run_id = self.campaign.runs[0].run_id
        review_path = self.root / "review.json"
        write_artifact(review_path, self._completed_review(run_id))
        raw_project = self.campaign_root / "runs" / run_id / "artifacts" / "repository"
        with self.assertRaisesRegex(ValidationError, "immutable raw run"):
            link_review_external_evidence(
                self.campaign_root,
                raw_project,
                review_path,
                reviewer_qualification="Licensed professional engineer",
            )

        derived = self.root / "derived-project"
        derived.mkdir()
        canonical_review = import_review(self.campaign_root, self.corpus, review_path)
        with mock.patch(
            "pcbdraft.verification.boardbench_evidence.record_external_evidence",
            return_value=derived / "external-evidence.json",
        ) as record:
            result = link_review_external_evidence(
                self.campaign_root,
                derived,
                canonical_review,
                reviewer_qualification="Licensed professional engineer",
            )
        self.assertEqual(result, derived / "external-evidence.json")
        self.assertEqual(record.call_args.kwargs["level"], "L6")
        self.assertEqual(record.call_args.kwargs["outcome"], "pass")


if __name__ == "__main__":
    unittest.main()
