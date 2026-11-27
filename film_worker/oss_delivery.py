"""OSS files and submission declarations for a leased machine assignment."""
import json
import os
import re
import shutil
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from . import bundles
from .protocol import OSS_OBJECTS, Interrupted, digest, opaque
from .secrets import oss_credentials
from .settings import atomic_json, object_hash


MAX_ARTIFACT = 16 * 1024**3
OBJECT_NAMES = {
    "source_timeline": "timeline.json", "review_index": "review-index.json",
    "version_manifest": "version.json", "shot_metadata": "meta.jsonl",
    "production_report": "production.json", "delivery_bundle": "delivery.zip",
}


class DeliveryError(ValueError):
    """A safe, actionable OSS error for the local and server job history."""


def transport(assignment):
    options = assignment.get("oss")
    return [options.get("protocol", "zip"), options["delivery"]] if options else None


def manual_archive(assignment, directory):
    direct_final = assignment["kind"] == "finalize" and assignment["oss"].get("protocol") == OSS_OBJECTS
    return directory / ("delivery.zip" if direct_final else "submission.zip")


def identity(assignment):
    return dict(job_id=assignment["job_id"], work_id=assignment["work_id"], attempt=assignment["lease"]["attempt"],
                input_manifest_sha256=assignment["input_manifest_sha256"])


def write_archive(target, files, manifest=None, *, stop):
    # A completed archive is reusable after restart, including its exact bytes and upload checkpoint.
    identity = object_hash([manifest, [[name, item[2]] for name, item in sorted(files.items())]])
    record = target.with_name(target.name + ".json")
    if record.is_file() and target.is_file():
        saved = json.loads(record.read_text(encoding="utf-8"))
        if saved["identity"] == identity and saved["size_bytes"] == target.stat().st_size and saved["sha256"] == digest(target):
            return
    if sum(item[0].stat().st_size for item in files.values()) > MAX_ARTIFACT:
        raise DeliveryError("交付 ZIP 展开后超过服务端 16 GiB 上限")
    temporary = target.with_suffix(".partial")
    try:
        with ZipFile(temporary, "w", ZIP_STORED, allowZip64=True) as archive:
            if manifest is not None:
                archive.writestr(ZipInfo("submission.json"), json.dumps(manifest, ensure_ascii=False).encode())
            for name, (path, _, _) in sorted(files.items()):
                if stop.is_set():
                    raise Interrupted
                bundles.safe_path(name)
                archive.write(path, name)
        if temporary.stat().st_size > MAX_ARTIFACT:
            raise DeliveryError("交付 ZIP 超过服务端 16 GiB 上限")
        temporary.replace(target)
        atomic_json(record, dict(identity=identity, sha256=digest(target), size_bytes=target.stat().st_size))
    finally:
        temporary.unlink(missing_ok=True)


def object_files(assignment, artifacts):
    files = {OBJECT_NAMES[role]: item for role, item in artifacts.items()}
    for name, (path, _, _) in files.items():
        limit = MAX_ARTIFACT if name == "delivery.zip" else 16 * 1024**2
        if not 0 < path.stat().st_size <= limit:
            raise DeliveryError(f"产物 {name} 大小超出服务端允许的范围")
    if assignment["kind"] == "finalize":
        return files
    path = files["review-index.json"][0]
    index = json.loads(path.read_text(encoding="utf-8"))
    for frame in index["frames"]:
        name = f"pairs/{frame['number']:06d}.jpg"
        pair = path.parent.parent / "review-bundle" / name
        if (not 0 < frame["size_bytes"] <= 14 * 1024**2
                or pair.stat().st_size != frame["size_bytes"] or digest(pair) != frame["sha256"]):
            raise DeliveryError(f"第 {frame['number']} 帧与已锁定的审核图摘要不符")
        files[name] = (pair, "image/jpeg", frame["sha256"])
    return files


def location(upload):
    region, bucket, key = (upload[name] for name in ("region", "bucket", "key"))
    if (not re.fullmatch(r"cn-[a-z0-9]+(?:-[a-z0-9]+)*", region)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket)
            or not re.fullmatch(r"movie-review/(incoming/work|jobs)/[A-Za-z0-9_./-]+", key)):
        raise DeliveryError("服务端 OSS 交付地址无效")
    bundles.safe_path(key)
    return region, bucket, key


def uploader_for(region, credentials, directory):
    import alibabacloud_oss_v2 as oss

    config = oss.config.load_default()
    config.region = region
    config.endpoint = f"https://oss-{region}.aliyuncs.com"
    config.signature_version = "v4"
    config.credentials_provider = oss.credentials.StaticCredentialsProvider(*credentials)
    config.connect_timeout = 10
    config.readwrite_timeout = 30
    directory.mkdir(exist_ok=True)
    uploader = oss.Client(config).uploader(part_size=32 * 1024**2, enable_checkpoint=True, parallel_num=2,
                                           leave_parts_on_error=True, checkpoint_dir=str(directory))
    return uploader, oss.PutObjectRequest


def upload_files(upload, files, root, directory, stop, progress, *, versioned):
    region, bucket, prefix = location(upload)
    ledger_path = directory / "oss-uploaded.json"
    fingerprint = object_hash([region, bucket, prefix])
    saved = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.is_file() else {}
    completed = saved.get("completed", {}) if saved.get("location") == fingerprint else {}
    uploader, request = uploader_for(region, oss_credentials(root), directory / "oss-multipart")
    total = sum(item[0].stat().st_size for item in files.values())
    done = 0
    result = {}

    def check_cancelled(*_):
        if stop.is_set():
            raise Interrupted

    for name, (path, media_type, sha256) in files.items():
        check_cancelled()
        length = path.stat().st_size
        spec = dict(size_bytes=length, sha256=sha256)
        previous = completed.get(name)
        if previous and all(previous[k] == value for k, value in spec.items()):
            declaration = previous
        else:
            key = f"{prefix}/{name}" if versioned else prefix
            try:
                response = uploader.upload_file(request(bucket=bucket, key=key, content_type=media_type,
                                                        metadata={"sha256": sha256}, progress_fn=check_cancelled), str(path))
            except Interrupted:
                raise
            except Exception as exc:
                check_cancelled()
                raise DeliveryError(f"OSS 上传失败（{type(exc).__name__}），请检查网络和 RAM 写入权限") from None
            declaration = spec
            if versioned:
                version_id = response.version_id
                if not isinstance(version_id, str) or version_id == "null" or not re.fullmatch(r"[A-Za-z0-9+/=._~-]{1,256}", version_id):
                    raise DeliveryError("OSS 未返回对象版本，请确认审核 Bucket 已启用版本控制")
                declaration = {**spec, "version_id": version_id}
            completed[name] = declaration
            atomic_json(ledger_path, dict(location=fingerprint, completed=completed))
        done += length
        progress(done, total)
        result[name] = declaration
    return result


def prepare_submission(assignment, artifacts, verification, root, directory, transfer, progress):
    options = assignment["oss"]
    objects = options.get("protocol") == OSS_OBJECTS
    payload = {"input_manifest_sha256": assignment["input_manifest_sha256"]}
    if objects:
        files = object_files(assignment, artifacts)
        if options["delivery"] == "auto":
            upload = transfer.json("POST", f"/work/{transfer.work}/oss/objects", payload, "oss-object-directory")
            payload["upload_id"] = opaque(upload["upload_id"])
            payload["objects"] = upload_files(upload, files, root, directory, transfer.stop, progress, versioned=True)
            if verification is not None:
                payload["final_verification"] = verification
            return payload
        if assignment["kind"] == "finalize":
            source = files["delivery.zip"][0]
            target = manual_archive(assignment, directory)
            if not target.exists() or digest(target) != files["delivery.zip"][2]:
                temporary = target.with_suffix(".partial")
                temporary.unlink(missing_ok=True)
                try:
                    try:
                        os.link(source, temporary)
                    except OSError:
                        shutil.copyfile(source, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
        else:
            write_archive(manual_archive(assignment, directory), files, stop=transfer.stop)
        return payload

    files = {f"out/{role}{item[0].suffix}": item for role, item in artifacts.items()}
    index = dict(identity(assignment), outputs={role: f"out/{role}{item[0].suffix}" for role, item in artifacts.items()})
    if verification is not None:
        index["final_verification"] = verification
    target = manual_archive(assignment, directory)
    write_archive(target, files, index, stop=transfer.stop)
    if options["delivery"] == "auto":
        item = (target, "application/zip", digest(target))
        payload.update(upload_files(options["upload"], {"submission.zip": item}, root, directory,
                                    transfer.stop, progress, versioned=False)["submission.zip"])
    return payload
