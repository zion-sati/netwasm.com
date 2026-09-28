#!/usr/bin/env python3
"""Build and verify immutable netwasm.com delivery evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import zipfile


SCRIPT_ROOT = Path(__file__).resolve().parent


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TRAIN = load("website_release_train", SCRIPT_ROOT / "release-train.py")
COORDINATOR = load(
    "website_release_coordinator", SCRIPT_ROOT / "release-coordinator.py"
)

CLOUDFLARE_SCRIPT = re.compile(r"<script\b[^>]*></script>\r?\n?")
CLOUDFLARE_ATTRIBUTE = re.compile(
    r"\s+([A-Za-z_:][\w:.-]*)\s*=\s*(\"[^\"]*\"|'[^']*')"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON document is not an object: {path}")
    return value


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def append_outputs(path: Path | None, values: dict[str, object]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            stream.write(f"{name}={value}\n")


def positive_integer(value: str | int, description: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{description} must be a positive integer.") from error
    if isinstance(value, bool) or parsed < 1 or str(parsed) != str(value):
        raise ValueError(f"{description} must be a positive integer.")
    return parsed


def safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or name.endswith("/")
    ):
        raise ValueError(f"Site archive path is unsafe: {name}")
    return path


def public_inventory(path: Path) -> list[PurePosixPath]:
    names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    if not names or any(not name or name.startswith("#") for name in names):
        raise ValueError("Public site inventory contains an empty or comment entry.")
    paths = [safe_archive_path(name) for name in names]
    if paths != sorted(paths, key=str) or len(paths) != len(set(paths)):
        raise ValueError("Public site inventory must be sorted and unique.")
    return paths


def build_site(
    source: Path,
    output: Path,
    inventory_path: Path,
    source_commit: str,
) -> dict[str, object]:
    if TRAIN.COMMIT.fullmatch(source_commit) is None:
        raise ValueError("Website source commit is invalid.")
    inventory = public_inventory(inventory_path)
    if output.exists():
        raise ValueError("Website output directory already exists.")
    files: dict[str, dict[str, object]] = {}
    for relative in inventory:
        source_path = source.joinpath(*relative.parts)
        if not source_path.is_file() or source_path.is_symlink():
            raise ValueError(f"Public site file is missing or not regular: {relative}")
        destination = output.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, destination)
        files[str(relative)] = {
            "bytes": destination.stat().st_size,
            "sha256": sha256(destination),
        }
    index = files.get("index.html")
    if index is None:
        raise ValueError("Public site inventory does not contain index.html.")
    identity = {
        "schemaVersion": 1,
        "sourceCommit": source_commit,
        "indexHtmlSha256": index["sha256"],
        "files": files,
    }
    write_json(output / "site-identity.json", identity)
    return identity


def verify_site(directory: Path) -> dict[str, object]:
    identity_path = directory / "site-identity.json"
    identity = read_json(identity_path)
    if set(identity) != {
        "schemaVersion", "sourceCommit", "indexHtmlSha256", "files",
    } or identity.get("schemaVersion") != 1:
        raise ValueError("Website identity fields are invalid.")
    if TRAIN.COMMIT.fullmatch(str(identity.get("sourceCommit", ""))) is None:
        raise ValueError("Website identity source commit is invalid.")
    files = identity.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Website identity file inventory is invalid.")
    expected_paths = sorted([*files, "site-identity.json"])
    actual_paths = sorted(
        str(path.relative_to(directory).as_posix())
        for path in directory.rglob("*")
        if path.is_file()
    )
    if actual_paths != expected_paths:
        raise ValueError("Website directory does not match its identity inventory.")
    for name, descriptor in files.items():
        safe_archive_path(name)
        path = directory / name
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"bytes", "sha256"}
            or not isinstance(descriptor.get("bytes"), int)
            or isinstance(descriptor.get("bytes"), bool)
            or descriptor["bytes"] < 0
            or not isinstance(descriptor.get("sha256"), str)
            or TRAIN.SHA256.fullmatch(str(descriptor["sha256"])) is None
            or path.stat().st_size != descriptor["bytes"]
            or sha256(path) != descriptor["sha256"]
        ):
            raise ValueError(f"Website file does not match its identity: {name}")
    index = files.get("index.html")
    if not isinstance(index, dict) or identity.get("indexHtmlSha256") != index.get("sha256"):
        raise ValueError("Website index identity is invalid.")
    return identity


def observed_index_html(path: Path) -> dict[str, str | None]:
    text = path.read_bytes().decode("utf-8")
    matches = [
        match
        for match in CLOUDFLARE_SCRIPT.finditer(text)
        if "https://static.cloudflareinsights.com/beacon.min.js/"
        in match.group(0)
    ]
    if len(matches) > 1:
        raise ValueError(
            "Live website contains multiple Cloudflare analytics injections."
        )
    transform = None
    if matches:
        match = matches[0]
        tag = match.group(0).removesuffix("\n").removesuffix("\r")
        opening = tag[len("<script"):tag.index(">")]
        attributes = list(CLOUDFLARE_ATTRIBUTE.finditer(opening))
        values = {
            attribute.group(1): attribute.group(2)[1:-1]
            for attribute in attributes
        }
        try:
            beacon = json.loads(values.get("data-cf-beacon", ""))
        except json.JSONDecodeError:
            beacon = None
        if (
            "".join(attribute.group(0) for attribute in attributes) != opening
            or len(attributes) != len(values)
            or sorted(values) != [
                "crossorigin", "data-cf-beacon", "integrity", "src", "type",
            ]
            or values.get("type") != "module"
            or values.get("crossorigin") != "anonymous"
            or re.fullmatch(
                r"https://static\.cloudflareinsights\.com/"
                r"beacon\.min\.js/[A-Za-z0-9]+",
                values.get("src", ""),
            ) is None
            or re.fullmatch(
                r"sha512-[A-Za-z0-9+/=]+", values.get("integrity", "")
            ) is None
            or not isinstance(beacon, dict)
            or not isinstance(beacon.get("version"), str)
            or not isinstance(beacon.get("token"), str)
            or not text[match.end():].startswith("</body>")
        ):
            raise ValueError("Cloudflare analytics injection is malformed.")
        text = text[:match.start()] + text[match.end():]
        transform = "cloudflare-web-analytics"
    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "edgeTransform": transform,
    }


def verify_observed_site(
    *,
    identity_path: Path,
    index_path: Path,
    files_directory: Path,
    expected_identity_sha256: str,
    expected_index_sha256: str,
) -> dict[str, str | None]:
    if (
        TRAIN.SHA256.fullmatch(expected_identity_sha256) is None
        or TRAIN.SHA256.fullmatch(expected_index_sha256) is None
    ):
        raise ValueError("Expected website identity is invalid.")
    if sha256(identity_path) != expected_identity_sha256:
        raise ValueError("Live website identity bytes differ from the candidate.")
    identity = read_json(identity_path)
    if set(identity) != {
        "schemaVersion", "sourceCommit", "indexHtmlSha256", "files",
    } or identity.get("schemaVersion") != 1:
        raise ValueError("Live website identity fields are invalid.")
    if TRAIN.COMMIT.fullmatch(str(identity.get("sourceCommit", ""))) is None:
        raise ValueError("Live website source commit is invalid.")
    if identity.get("indexHtmlSha256") != expected_index_sha256:
        raise ValueError("Live website index identity differs from the candidate.")
    files = identity.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Live website file inventory is invalid.")
    observed = observed_index_html(index_path)
    if observed["sha256"] != expected_index_sha256:
        raise ValueError("Live website HTML differs from the candidate.")
    expected_paths = sorted(name for name in files if name != "index.html")
    actual_paths = sorted(
        str(path.relative_to(files_directory).as_posix())
        for path in files_directory.rglob("*")
        if path.is_file()
    )
    if actual_paths != expected_paths:
        raise ValueError("Observed live website files do not match the inventory.")
    for name in expected_paths:
        safe_archive_path(name)
        descriptor = files[name]
        path = files_directory / name
        if (
            not isinstance(descriptor, dict)
            or set(descriptor) != {"bytes", "sha256"}
            or not isinstance(descriptor.get("bytes"), int)
            or isinstance(descriptor.get("bytes"), bool)
            or descriptor["bytes"] < 0
            or not isinstance(descriptor.get("sha256"), str)
            or TRAIN.SHA256.fullmatch(str(descriptor["sha256"])) is None
            or path.is_symlink()
            or path.stat().st_size != descriptor["bytes"]
            or sha256(path) != descriptor["sha256"]
        ):
            raise ValueError(f"Live website file differs from the candidate: {name}")
    return observed


def input_context(
    preparation_path: Path,
    inputs_path: Path,
    kind: str,
) -> tuple[
    dict[str, object], str, dict[str, object], dict[str, object],
    list[dict[str, str]],
]:
    if kind not in {"website-site-candidate", "website-completion"}:
        raise ValueError("Website delivery receipt kind is invalid.")
    preparation, digest = TRAIN.read_preparation(preparation_path)
    inputs = read_json(inputs_path)
    stage = TRAIN.preparation_stage(preparation, "website")
    _, anchor_release_id = TRAIN.stage_state_anchor(preparation, "website")
    if (
        inputs.get("coordinated_stage") != "website"
        or inputs.get("preparation_sha256") != digest
        or inputs.get("release_id") != ""
        or inputs.get("release_ref") != stage["ref"]
        or inputs.get("source_commit") != stage["sourceCommit"]
        or inputs.get("infrastructure_commit") != stage["infrastructureCommit"]
        or inputs.get("stage_identity")
        != COORDINATOR.stage_identity(digest, "website")
        or inputs.get("preparation_release_id") != str(anchor_release_id)
    ):
        raise ValueError("Website delivery inputs do not match preparation.")
    try:
        value = json.loads(str(inputs["upstream_receipts"]))
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("Website upstream inputs are invalid.") from error
    if (
        not isinstance(value, list)
        or any(not isinstance(item, dict) for item in value)
        or [item.get("stage") for item in value] != ["playground"]
    ):
        raise ValueError("Website upstream input chain is invalid.")
    upstream: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"stage", "sha256", "fileName"}:
            raise ValueError("Website upstream input fields are invalid.")
        digest_value = item.get("sha256")
        file_name = item.get("fileName")
        if (
            not isinstance(digest_value, str)
            or TRAIN.SHA256.fullmatch(digest_value) is None
            or not isinstance(file_name, str)
            or Path(file_name).name != file_name
        ):
            raise ValueError("Website upstream input identity is invalid.")
        upstream.append({"stage": "playground", "sha256": digest_value})
    return preparation, digest, inputs, stage, upstream


def delivery_envelope(
    preparation_path: Path,
    inputs_path: Path,
    kind: str,
    *,
    run_id: int,
    run_attempt: int,
    job_id: int,
    actor_id: int,
) -> tuple[dict[str, object], dict[str, object], str, dict[str, object]]:
    preparation, digest, inputs, stage, upstream = input_context(
        preparation_path, inputs_path, kind
    )
    anchor_repository, anchor_release_id = TRAIN.stage_state_anchor(
        preparation, "website"
    )
    dispatch_identity = inputs.get("dispatch_attempt_identity")
    if not isinstance(dispatch_identity, str) or TRAIN.SHA256.fullmatch(dispatch_identity) is None:
        raise ValueError("Website dispatch identity is invalid.")
    envelope = {
        "schemaVersion": TRAIN.DELIVERY_RECEIPT_SCHEMA_VERSION,
        "kind": kind,
        "stage": "website",
        "preparationSha256": digest,
        "stageIdentity": TRAIN.release_stage_identity(digest, "website"),
        "repository": stage["repository"],
        "sourceCommit": stage["sourceCommit"],
        "infrastructureCommit": stage["infrastructureCommit"],
        "workflowCommit": stage["workflowCommit"],
        "workflowRef": stage["workflowRef"],
        "stateAnchor": {
            "repository": anchor_repository,
            "releaseId": anchor_release_id,
        },
        "upstreamReceipts": upstream,
        "producer": {
            "runId": run_id,
            "runAttempt": run_attempt,
            "jobId": job_id,
            "workflowPath": stage["workflow"],
            "actorId": actor_id,
            "dispatchAttemptIdentity": dispatch_identity,
        },
    }
    return envelope, preparation, digest, inputs


def upstream_bytes(
    receipt: dict[str, object],
    inputs_path: Path,
    upstream_directory: Path,
) -> dict[str, bytes]:
    inputs = read_json(inputs_path)
    coordinates = json.loads(str(inputs["upstream_receipts"]))
    result: dict[str, bytes] = {}
    for item in coordinates:
        path = upstream_directory / str(item["fileName"])
        stage = str(item["stage"])
        digest = str(item["sha256"])
        if not path.is_file() or sha256(path) != digest or stage in result:
            raise ValueError("Website upstream receipt bytes do not match.")
        result[stage] = path.read_bytes()
    expected = {
        str(item["stage"]): str(item["sha256"])
        for item in receipt.get("upstreamReceipts", [])
        if isinstance(item, dict)
    }
    actual = {
        stage: hashlib.sha256(contents).hexdigest()
        for stage, contents in result.items()
    }
    if expected != actual:
        raise ValueError("Website upstream receipt chain does not match its bytes.")
    return result


def candidate_receipt(
    *,
    preparation_path: Path,
    inputs_path: Path,
    archive_path: Path,
    artifact_id: int,
    run_id: int,
    run_attempt: int,
    job_id: int,
    actor_id: int,
    site_identity_path: Path,
) -> dict[str, object]:
    if not archive_path.is_file() or archive_path.stat().st_size < 1:
        raise ValueError("Website candidate archive is missing or empty.")
    envelope, preparation, digest, _ = delivery_envelope(
        preparation_path,
        inputs_path,
        "website-site-candidate",
        run_id=run_id,
        run_attempt=run_attempt,
        job_id=job_id,
        actor_id=actor_id,
    )
    identity = read_json(site_identity_path)
    index_digest = identity.get("indexHtmlSha256")
    if not isinstance(index_digest, str) or TRAIN.SHA256.fullmatch(index_digest) is None:
        raise ValueError("Website index identity is invalid.")
    receipt = {
        **envelope,
        "archive": {
            "fileName": archive_path.name,
            "bytes": archive_path.stat().st_size,
            "sha256": sha256(archive_path),
        },
        "artifact": {
            "repository": envelope["repository"],
            "runId": run_id,
            "runAttempt": run_attempt,
            "artifactId": artifact_id,
            "artifactName": TRAIN.delivery_candidate_payload_artifact_name(
                "website-site-candidate", run_id, run_attempt
            ),
        },
        "site": {
            "identitySha256": sha256(site_identity_path),
            "indexHtmlSha256": index_digest,
        },
    }
    TRAIN.validate_candidate_receipt(
        receipt, preparation, digest, "website-site-candidate"
    )
    return receipt


def verify_candidate(
    *,
    preparation_path: Path,
    inputs_path: Path,
    receipt_path: Path,
    archive_path: Path,
    upstream_directory: Path,
    expected_receipt_sha256: str | None = None,
) -> dict[str, object]:
    receipt = read_json(receipt_path)
    preparation, digest, _, _, _ = input_context(
        preparation_path, inputs_path, "website-site-candidate"
    )
    TRAIN.validate_candidate_receipt(
        receipt, preparation, digest, "website-site-candidate"
    )
    receipt_digest = sha256(receipt_path)
    if expected_receipt_sha256 is not None and receipt_digest != expected_receipt_sha256:
        raise ValueError("Website candidate receipt digest does not match retained input.")
    archive = receipt["archive"]
    assert isinstance(archive, dict)
    if (
        archive_path.name != archive.get("fileName")
        or not archive_path.is_file()
        or archive_path.stat().st_size != archive.get("bytes")
        or sha256(archive_path) != archive.get("sha256")
    ):
        raise ValueError("Website candidate archive does not match its receipt.")
    upstream_bytes(receipt, inputs_path, upstream_directory)
    return receipt


def pack_directory(source: Path, output: Path) -> None:
    verify_site(source)
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise ValueError("Website site directory is empty.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            safe_archive_path(relative)
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, path.read_bytes())


def extract_directory(archive_path: Path, output: Path) -> None:
    if output.exists():
        raise ValueError("Website extraction directory already exists.")
    output.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        if not names or len(names) != len(set(names)):
            raise ValueError("Website archive inventory is empty or duplicated.")
        for name in names:
            relative = safe_archive_path(name)
            info = archive.getinfo(name)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError(f"Website archive member is not regular: {name}")
            destination = output.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))
    verify_site(output)


def live_evidence(
    *,
    preparation_path: Path,
    inputs_path: Path,
    deployment_path: Path,
    run_id: int,
    run_attempt: int,
    job_id: int,
    site_identity_sha256: str,
    playground_completion_sha256: str,
) -> dict[str, object]:
    _, _, inputs, stage, _ = input_context(
        preparation_path, inputs_path, "website-completion"
    )
    deployment = read_json(deployment_path)
    expected = {
        "id", "environment", "url", "runId", "runAttempt", "jobId",
    }
    if set(deployment) != expected:
        raise ValueError("Website deployment fields are invalid.")
    return {
        "schemaVersion": 1,
        "status": "PASS",
        "stage": "website",
        "repository": stage["repository"],
        "runId": run_id,
        "runAttempt": run_attempt,
        "jobId": job_id,
        "dispatchAttemptIdentity": inputs["dispatch_attempt_identity"],
        "deployment": deployment,
        "siteIdentitySha256": site_identity_sha256,
        "playgroundCompletionSha256": playground_completion_sha256,
    }


def completion_receipt(
    *,
    preparation_path: Path,
    inputs_path: Path,
    metadata_path: Path,
    site_receipt_path: Path,
    upstream_directory: Path,
    run_id: int,
    run_attempt: int,
    actor_id: int,
) -> dict[str, object]:
    metadata = read_json(metadata_path)
    producer_job_id = positive_integer(
        metadata.get("producerJobId", 0), "Completion producer job ID"
    )
    envelope, preparation, digest, _ = delivery_envelope(
        preparation_path,
        inputs_path,
        "website-completion",
        run_id=run_id,
        run_attempt=run_attempt,
        job_id=producer_job_id,
        actor_id=actor_id,
    )
    site_receipt = read_json(site_receipt_path)
    TRAIN.validate_candidate_receipt(
        site_receipt, preparation, digest, "website-site-candidate"
    )
    upstream = upstream_bytes(site_receipt, inputs_path, upstream_directory)
    playground_digest = hashlib.sha256(upstream["playground"]).hexdigest()
    live_check = metadata.get("liveCheck")
    if not isinstance(live_check, dict):
        raise ValueError("Website live-check metadata is invalid.")
    if live_check.get("playgroundCompletionSha256") != playground_digest:
        raise ValueError("Website live check does not bind the Playground completion.")
    receipt = {
        **envelope,
        "status": "PASS",
        "siteCandidateSha256": sha256(site_receipt_path),
        "deployment": metadata.get("deployment"),
        "liveCheck": live_check,
    }
    TRAIN.validate_completion_receipt(
        receipt, preparation, digest, "website-completion"
    )
    return receipt


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    build = commands.add_parser("build-site")
    build.add_argument("--source", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--inventory", type=Path, required=True)
    build.add_argument("--source-commit", required=True)
    build.add_argument("--github-output", type=Path)

    verify_site_command = commands.add_parser("verify-site")
    verify_site_command.add_argument("--site", type=Path, required=True)
    verify_site_command.add_argument("--github-output", type=Path)

    observed = commands.add_parser("verify-observed-site")
    observed.add_argument("--identity", type=Path, required=True)
    observed.add_argument("--index", type=Path, required=True)
    observed.add_argument("--files", type=Path, required=True)
    observed.add_argument("--expected-identity-sha256", required=True)
    observed.add_argument("--expected-index-sha256", required=True)
    observed.add_argument("--github-output", type=Path)

    pack = commands.add_parser("pack-site")
    pack.add_argument("--source", type=Path, required=True)
    pack.add_argument("--output", type=Path, required=True)

    extract = commands.add_parser("extract-site")
    extract.add_argument("--archive", type=Path, required=True)
    extract.add_argument("--output", type=Path, required=True)

    candidate = commands.add_parser("candidate")
    candidate.add_argument("--preparation", type=Path, required=True)
    candidate.add_argument("--inputs", type=Path, required=True)
    candidate.add_argument("--archive", type=Path, required=True)
    candidate.add_argument("--artifact-id", type=int, required=True)
    candidate.add_argument("--run-id", type=int, required=True)
    candidate.add_argument("--run-attempt", type=int, required=True)
    candidate.add_argument("--job-id", type=int, required=True)
    candidate.add_argument("--actor-id", type=int, required=True)
    candidate.add_argument("--site-identity", type=Path, required=True)
    candidate.add_argument("--output", type=Path, required=True)
    candidate.add_argument("--github-output", type=Path)

    verify = commands.add_parser("verify-candidate")
    verify.add_argument("--preparation", type=Path, required=True)
    verify.add_argument("--inputs", type=Path, required=True)
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--archive", type=Path, required=True)
    verify.add_argument("--upstream-directory", type=Path, required=True)
    verify.add_argument("--expected-receipt-sha256")
    verify.add_argument("--github-output", type=Path)

    evidence = commands.add_parser("live-evidence")
    evidence.add_argument("--preparation", type=Path, required=True)
    evidence.add_argument("--inputs", type=Path, required=True)
    evidence.add_argument("--deployment", type=Path, required=True)
    evidence.add_argument("--run-id", type=int, required=True)
    evidence.add_argument("--run-attempt", type=int, required=True)
    evidence.add_argument("--job-id", type=int, required=True)
    evidence.add_argument("--site-identity-sha256", required=True)
    evidence.add_argument("--playground-completion-sha256", required=True)
    evidence.add_argument("--output", type=Path, required=True)

    completion = commands.add_parser("completion")
    completion.add_argument("--preparation", type=Path, required=True)
    completion.add_argument("--inputs", type=Path, required=True)
    completion.add_argument("--metadata", type=Path, required=True)
    completion.add_argument("--site-receipt", type=Path, required=True)
    completion.add_argument("--upstream-directory", type=Path, required=True)
    completion.add_argument("--run-id", type=int, required=True)
    completion.add_argument("--run-attempt", type=int, required=True)
    completion.add_argument("--actor-id", type=int, required=True)
    completion.add_argument("--output", type=Path, required=True)
    return root


def main() -> int:
    arguments = parser().parse_args()
    if arguments.command == "build-site":
        identity = build_site(
            arguments.source,
            arguments.output,
            arguments.inventory,
            arguments.source_commit,
        )
        append_outputs(arguments.github_output, {
            "index_html_sha256": identity["indexHtmlSha256"],
            "site_identity_sha256": sha256(arguments.output / "site-identity.json"),
        })
    elif arguments.command == "verify-site":
        identity = verify_site(arguments.site)
        append_outputs(arguments.github_output, {
            "index_html_sha256": identity["indexHtmlSha256"],
            "site_identity_sha256": sha256(arguments.site / "site-identity.json"),
        })
    elif arguments.command == "verify-observed-site":
        observation = verify_observed_site(
            identity_path=arguments.identity,
            index_path=arguments.index,
            files_directory=arguments.files,
            expected_identity_sha256=arguments.expected_identity_sha256,
            expected_index_sha256=arguments.expected_index_sha256,
        )
        append_outputs(arguments.github_output, {
            "edge_transform": observation["edgeTransform"] or "none",
        })
    elif arguments.command == "pack-site":
        pack_directory(arguments.source, arguments.output)
    elif arguments.command == "extract-site":
        extract_directory(arguments.archive, arguments.output)
    elif arguments.command == "candidate":
        value = candidate_receipt(
            preparation_path=arguments.preparation,
            inputs_path=arguments.inputs,
            archive_path=arguments.archive,
            artifact_id=arguments.artifact_id,
            run_id=arguments.run_id,
            run_attempt=arguments.run_attempt,
            job_id=arguments.job_id,
            actor_id=arguments.actor_id,
            site_identity_path=arguments.site_identity,
        )
        write_json(arguments.output, value)
        append_outputs(arguments.github_output, {"receipt_sha256": sha256(arguments.output)})
    elif arguments.command == "verify-candidate":
        value = verify_candidate(
            preparation_path=arguments.preparation,
            inputs_path=arguments.inputs,
            receipt_path=arguments.receipt,
            archive_path=arguments.archive,
            upstream_directory=arguments.upstream_directory,
            expected_receipt_sha256=arguments.expected_receipt_sha256,
        )
        site = value["site"]
        assert isinstance(site, dict)
        archive = value["archive"]
        assert isinstance(archive, dict)
        append_outputs(arguments.github_output, {
            "archive_name": archive["fileName"],
            "index_html_sha256": site["indexHtmlSha256"],
            "receipt_sha256": sha256(arguments.receipt),
            "site_identity_sha256": site["identitySha256"],
        })
    elif arguments.command == "live-evidence":
        write_json(arguments.output, live_evidence(
            preparation_path=arguments.preparation,
            inputs_path=arguments.inputs,
            deployment_path=arguments.deployment,
            run_id=arguments.run_id,
            run_attempt=arguments.run_attempt,
            job_id=arguments.job_id,
            site_identity_sha256=arguments.site_identity_sha256,
            playground_completion_sha256=arguments.playground_completion_sha256,
        ))
    elif arguments.command == "completion":
        write_json(arguments.output, completion_receipt(
            preparation_path=arguments.preparation,
            inputs_path=arguments.inputs,
            metadata_path=arguments.metadata,
            site_receipt_path=arguments.site_receipt,
            upstream_directory=arguments.upstream_directory,
            run_id=arguments.run_id,
            run_attempt=arguments.run_attempt,
            actor_id=arguments.actor_id,
        ))
    else:
        raise AssertionError(arguments.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
