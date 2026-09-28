import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DELIVERY = load("test_website_delivery_module", ROOT / "eng/website-delivery.py")
ACTIONS = load("test_website_actions_module", ROOT / "eng/actions-delivery.py")
TRAIN = DELIVERY.TRAIN
COORDINATOR = DELIVERY.COORDINATOR


class WebsiteFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        commits = {
            "zion-sati/NetWasm": "a" * 40,
            "zion-sati/TUnit-NetWasm": "b" * 40,
            "zion-sati/NetWasm.Libraries": "c" * 40,
            "zion-sati/NetWasm.Playground": "d" * 40,
            "zion-sati/netwasm.com": "e" * 40,
        }
        release_ids = iter(range(101, 108))
        stages = []
        for name, repository, workflow, upstream, _ in TRAIN.PREPARATION_STAGES:
            website = name == "website"
            stages.append({
                "name": name,
                "repository": repository,
                "workflow": workflow,
                "sourceCommit": commits[repository],
                "infrastructureCommit": commits[repository],
                "workflowCommit": commits[repository],
                "workflowRef": "main" if website else TRAIN.expected_stage_ref(
                    name, "0.5.0"
                ),
                "ref": commits[repository] if website else TRAIN.expected_stage_ref(
                    name, "0.5.0"
                ),
                "releaseId": None if website else next(release_ids),
                "prerelease": name.endswith("-preview"),
                "upstreamStages": list(upstream),
            })
        self.preparation_value = {
            "schemaVersion": 1,
            "version": "0.5.0",
            "stages": stages,
            "policy": {
                "publicationReceiptSchemaVersion": 1,
                "completionStage": "website",
            },
        }
        self.preparation = self.root / "release-preparation.json"
        DELIVERY.write_json(self.preparation, self.preparation_value)
        self.digest = DELIVERY.sha256(self.preparation)
        stage = TRAIN.preparation_stage(self.preparation_value, "website")
        anchor = TRAIN.preparation_stage(self.preparation_value, "core-preview")
        self.playground = self.root / "upstream" / "playground.json"
        DELIVERY.write_json(self.playground, {"stage": "playground"})
        self.inputs_value = {
            "coordinated_stage": "website",
            "preparation_sha256": self.digest,
            "release_id": "",
            "release_ref": stage["ref"],
            "source_commit": stage["sourceCommit"],
            "infrastructure_commit": stage["infrastructureCommit"],
            "coordinator_repository": "zion-sati/NetWasm",
            "coordinator_run_id": "600",
            "coordinator_run_attempt": "2",
            "stage_identity": COORDINATOR.stage_identity(self.digest, "website"),
            "dispatch_attempt_identity": COORDINATOR.dispatch_attempt_identity(
                self.digest, "website", "600", "2"
            ),
            "preparation_release_id": str(anchor["releaseId"]),
            "upstream_receipts": json.dumps([{
                "stage": "playground",
                "sha256": DELIVERY.sha256(self.playground),
                "fileName": self.playground.name,
            }], separators=(",", ":"), sort_keys=True),
            "retained_candidates": "[]",
        }
        self.inputs = self.root / "inputs.json"
        DELIVERY.write_json(self.inputs, self.inputs_value)
        self.site = self.root / "site"
        self.identity = DELIVERY.build_site(
            ROOT,
            self.site,
            ROOT / "eng/public-files.txt",
            "e" * 40,
        )
        self.archive = self.root / "website.zip"
        DELIVERY.pack_directory(self.site, self.archive)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def candidate(self) -> Path:
        value = DELIVERY.candidate_receipt(
            preparation_path=self.preparation,
            inputs_path=self.inputs,
            archive_path=self.archive,
            artifact_id=810,
            run_id=700,
            run_attempt=1,
            job_id=900,
            actor_id=12345,
            site_identity_path=self.site / "site-identity.json",
        )
        path = self.root / "site-receipt.json"
        DELIVERY.write_json(path, value)
        return path


class WebsiteDeliveryTests(WebsiteFixture):
    def observed_files(self) -> Path:
        observed = self.root / "observed-files"
        observed.mkdir()
        for name in self.identity["files"]:
            if name == "index.html":
                continue
            source = self.site / name
            destination = observed / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        return observed

    def verify_observed(self, index: Path, files: Path) -> dict[str, str | None]:
        return DELIVERY.verify_observed_site(
            identity_path=self.site / "site-identity.json",
            index_path=index,
            files_directory=files,
            expected_identity_sha256=DELIVERY.sha256(
                self.site / "site-identity.json"
            ),
            expected_index_sha256=self.identity["indexHtmlSha256"],
        )

    def test_explicit_site_inventory_and_archive_are_reproducible(self) -> None:
        self.assertNotIn("README.md", self.identity["files"])
        self.assertNotIn("LICENSE", self.identity["files"])
        self.assertEqual(
            hashlib.sha256((self.site / "index.html").read_bytes()).hexdigest(),
            self.identity["indexHtmlSha256"],
        )
        second = self.root / "second.zip"
        DELIVERY.pack_directory(self.site, second)
        self.assertEqual(self.archive.read_bytes(), second.read_bytes())
        extracted = self.root / "extracted"
        DELIVERY.extract_directory(self.archive, extracted)
        self.assertEqual(
            (self.site / "site-identity.json").read_bytes(),
            (extracted / "site-identity.json").read_bytes(),
        )
        (extracted / "styles.css").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            DELIVERY.verify_site(extracted)

    def test_candidate_and_completion_bind_exact_site_and_playground(self) -> None:
        candidate = self.candidate()
        DELIVERY.verify_candidate(
            preparation_path=self.preparation,
            inputs_path=self.inputs,
            receipt_path=candidate,
            archive_path=self.archive,
            upstream_directory=self.playground.parent,
        )
        deployment = {
            "id": 1001,
            "environment": "github-pages",
            "url": "https://www.netwasm.com/",
            "runId": 700,
            "runAttempt": 1,
            "jobId": 902,
        }
        metadata = self.root / "metadata.json"
        DELIVERY.write_json(metadata, {
            "producerJobId": 903,
            "deployment": deployment,
            "liveCheck": {
                "status": "PASS",
                "jobId": 904,
                "siteIdentitySha256": DELIVERY.sha256(
                    self.site / "site-identity.json"
                ),
                "playgroundCompletionSha256": DELIVERY.sha256(self.playground),
                "evidence": {
                    "artifactId": 950,
                    "artifactName": TRAIN.delivery_evidence_artifact_name(
                        "website-completion", 700, 1, 904
                    ),
                    "fileName": "delivery-evidence.json",
                    "sha256": "8" * 64,
                },
            },
        })
        receipt = DELIVERY.completion_receipt(
            preparation_path=self.preparation,
            inputs_path=self.inputs,
            metadata_path=metadata,
            site_receipt_path=candidate,
            upstream_directory=self.playground.parent,
            run_id=700,
            run_attempt=1,
            actor_id=12345,
        )
        TRAIN.validate_completion_receipt(
            receipt,
            self.preparation_value,
            self.digest,
            "website-completion",
        )

    def test_live_observation_checks_html_and_every_public_file(self) -> None:
        files = self.observed_files()
        observation = self.verify_observed(self.site / "index.html", files)
        self.assertIsNone(observation["edgeTransform"])
        (files / "styles.css").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "file differs"):
            self.verify_observed(self.site / "index.html", files)

    def test_live_observation_rejects_stale_or_changed_html(self) -> None:
        files = self.observed_files()
        original = (self.site / "index.html").read_text(encoding="utf-8")
        stale = self.root / "stale.html"
        stale.write_text(original.replace("<body", "<body data-stale=\"true\"", 1))
        with self.assertRaisesRegex(ValueError, "HTML differs"):
            self.verify_observed(stale, files)

        injection = (
            '<script type="module" '
            'src="https://static.cloudflareinsights.com/beacon.min.js/v31edd6" '
            'integrity="sha512-aA==" '
            'data-cf-beacon=\'{"version":"2024.11.0","token":"public"}\' '
            'crossorigin="anonymous"></script>\n'
        )
        changed = self.root / "changed-with-marker.html"
        changed.write_text(
            original.replace("</body>", f"<p>changed</p>{injection}</body>"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "HTML differs"):
            self.verify_observed(changed, files)

    def test_live_observation_allows_only_exact_cloudflare_injection(self) -> None:
        files = self.observed_files()
        original = (self.site / "index.html").read_text(encoding="utf-8")
        injection = (
            '<script type="module" '
            'src="https://static.cloudflareinsights.com/beacon.min.js/v31edd6" '
            'integrity="sha512-aA==" '
            'data-cf-beacon=\'{"version":"2024.11.0","token":"public"}\' '
            'crossorigin="anonymous"></script>\n'
        )
        transformed = self.root / "cloudflare.html"
        transformed.write_text(
            original.replace("</body>", f"{injection}</body>"),
            encoding="utf-8",
        )
        self.assertEqual(
            "cloudflare-web-analytics",
            self.verify_observed(transformed, files)["edgeTransform"],
        )

        altered = self.root / "altered-injection.html"
        altered.write_text(
            original.replace(
                "</body>",
                injection.replace(
                    ' crossorigin="anonymous"',
                    ' onload="bad" crossorigin="anonymous"',
                ) + "</body>",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "injection is malformed"):
            self.verify_observed(altered, files)

        displaced = self.root / "displaced-injection.html"
        displaced.write_text(
            original.replace("</head>", f"{injection}</head>"),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "injection is malformed"):
            self.verify_observed(displaced, files)


class WebsiteActionsTests(WebsiteFixture):
    def job(
        self,
        job_id: int,
        name: str,
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> dict[str, object]:
        return {
            "id": job_id,
            "name": name,
            "run_id": 700,
            "run_attempt": 1,
            "head_sha": "e" * 40,
            "status": status,
            "conclusion": conclusion,
        }

    def test_resolves_exact_deployment_and_live_evidence(self) -> None:
        jobs = self.root / "jobs.json"
        DELIVERY.write_json(jobs, {"jobs": [
            self.job(901, "complete-delivery", "in_progress", None),
            self.job(902, "deploy"),
            self.job(903, "verify-live"),
        ]})
        deployments = self.root / "deployments.json"
        DELIVERY.write_json(deployments, [{
            "id": 1001,
            "sha": "e" * 40,
            "ref": "main",
            "task": "deploy",
            "environment": "github-pages",
            "creator": {"id": 12345},
        }])
        statuses = self.root / "statuses"
        DELIVERY.write_json(statuses / "1001.json", [{
            "state": "success",
            "environment": "github-pages",
            "environment_url": "https://www.netwasm.com/",
            "log_url": "https://github.com/zion-sati/netwasm.com/actions/runs/700/job/902",
            "creator": {"id": 12345},
        }])
        deployment = ACTIONS.deployment_record(
            jobs_path=jobs,
            deployments_path=deployments,
            statuses_directory=statuses,
            repository="zion-sati/netwasm.com",
            run_id=700,
            run_attempt=1,
            job_name="deploy",
            head_sha="e" * 40,
            workflow_ref="main",
            actor_id=12345,
            expected_url="https://www.netwasm.com/",
        )
        deployment_path = self.root / "deployment.json"
        DELIVERY.write_json(deployment_path, deployment)
        evidence = DELIVERY.live_evidence(
            preparation_path=self.preparation,
            inputs_path=self.inputs,
            deployment_path=deployment_path,
            run_id=700,
            run_attempt=1,
            job_id=903,
            site_identity_sha256=DELIVERY.sha256(self.site / "site-identity.json"),
            playground_completion_sha256=DELIVERY.sha256(self.playground),
        )
        evidence_path = self.root / "delivery-evidence.json"
        DELIVERY.write_json(evidence_path, evidence)
        artifact_name = TRAIN.delivery_evidence_artifact_name(
            "website-completion", 700, 1, 903
        )
        artifacts = self.root / "artifacts.json"
        DELIVERY.write_json(artifacts, {"artifacts": [{
            "id": 950,
            "name": artifact_name,
            "expired": False,
            "workflow_run": {"id": 700, "head_sha": "e" * 40},
        }]})
        metadata = ACTIONS.completion_metadata(
            jobs_path=jobs,
            artifacts_path=artifacts,
            evidence_path=evidence_path,
            deployment_path=deployment_path,
            repository="zion-sati/netwasm.com",
            run_id=700,
            run_attempt=1,
            producer_job_name="complete-delivery",
            live_job_name="verify-live",
            head_sha="e" * 40,
            dispatch_attempt_identity=self.inputs_value["dispatch_attempt_identity"],
            site_identity_sha256=DELIVERY.sha256(self.site / "site-identity.json"),
            playground_completion_sha256=DELIVERY.sha256(self.playground),
        )
        self.assertEqual(901, metadata["producerJobId"])
        self.assertEqual(950, metadata["liveCheck"]["evidence"]["artifactId"])

    def test_tag_resolution_rejects_non_commit_tags(self) -> None:
        repository = self.root / "repository"
        repository.mkdir()

        def git(*arguments: str) -> str:
            result = subprocess.run(
                ["git", *arguments], cwd=repository, check=True,
                capture_output=True, text=True,
            )
            return result.stdout.strip()

        git("init", "--initial-branch=main")
        git("config", "user.name", "Zion Sati")
        git("config", "user.email", "283163728+zion-sati@users.noreply.github.com")
        (repository / "file").write_text("content\n", encoding="utf-8")
        git("add", "file")
        git("commit", "-m", "Create fixture")
        commit = git("rev-parse", "HEAD")
        git("tag", "v0.5.0")
        blob = git("hash-object", "-w", "file")
        git("tag", "blob", blob)
        self.assertEqual(commit, ACTIONS.tag_commit(repository, "v0.5.0"))
        self.assertEqual("", ACTIONS.tag_commit(repository, "absent"))
        with self.assertRaisesRegex(ValueError, "does not resolve to a commit"):
            ACTIONS.tag_commit(repository, "blob")


class WorkflowContractTests(unittest.TestCase):
    def test_production_is_authenticated_dispatch_only(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("push:\n", workflow)
        self.assertIn("python3 bootstrap/eng/release-receiver.py", workflow)
        self.assertIn("--approved-actor-id \"$APPROVED_ACTOR_ID\"", workflow)
        self.assertIn("run-name: Release website ${{ inputs.stage_identity }}", workflow)
        self.assertEqual(
            8,
            workflow.count("Native production reruns are not supported"),
        )

    def test_candidate_and_completion_are_final_producer_outputs(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertGreater(
            workflow.index("- name: Retain website candidate receipt"),
            workflow.index("- name: Retain resolved website for deployment"),
        )
        self.assertTrue(
            workflow.rstrip().endswith("retention-days: 90"),
            "Completion receipt upload must remain the final completion step.",
        )

    def test_deployment_and_transport_are_attempt_bound(self) -> None:
        workflow = (ROOT / ".github/workflows/pages.yml").read_text()
        self.assertIn("bind-deployment:", workflow)
        self.assertIn("needs: [receive, deploy]", workflow)
        self.assertIn("needs: [receive, site-candidate, deploy, bind-deployment]", workflow)
        self.assertIn("github-pages-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("artifact_name: ${{ needs.stage-site.outputs.pages_artifact }}", workflow)
        self.assertIn("verify-observed-site", workflow)
        self.assertIn(".files | keys[] | select(. != \"index.html\")", workflow)

    def test_public_inventory_is_explicit_and_excludes_repository_documents(self) -> None:
        inventory = [
            str(path) for path in DELIVERY.public_inventory(ROOT / "eng/public-files.txt")
        ]
        self.assertIn("index.html", inventory)
        self.assertIn("CNAME", inventory)
        self.assertNotIn(".nojekyll", inventory)
        self.assertNotIn("README.md", inventory)
        self.assertNotIn("LICENSE", inventory)


if __name__ == "__main__":
    unittest.main()
