"""Neon S3-compatible Object Storage; only the backend receives credentials.

Local uploads are a disposable cache. Each file is uploaded BEFORE the product
is committed, and is restored from Object Storage after a Render restart.
"""
import logging
import mimetypes
import os
import re
import tempfile
from contextlib import closing
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger(__name__)
NAME_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]{2,5}$")
MAX_FILE_BYTES = 50 * 1024 * 1024


def enabled():
    return bool(os.getenv("AWS_ENDPOINT_URL_S3", "").strip())


def _client():
    endpoint = os.environ["AWS_ENDPOINT_URL_S3"].strip().rstrip("/")
    parsed = urlparse(endpoint)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise RuntimeError("AWS_ENDPOINT_URL_S3 должен быть HTTPS endpoint из Neon Connect → Storage")
    return boto3.client(
        "s3",
        region_name=os.environ["AWS_REGION"].strip(),
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"].strip(),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "max_attempts": 2},
        ),
    )


def _bucket():
    bucket = os.getenv("STORAGE_BUCKET", "autoprime").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("Некорректное имя STORAGE_BUCKET. Используйте autoprime")
    return bucket


def _name(name):
    if not NAME_RE.fullmatch(name):
        raise ValueError("Некорректное имя медиа")
    return name


def _error(exc):
    # Do not expose provider messages, signed requests or credentials to clients.
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(exc, ClientError) else None
    if status in (401, 403):
        message = "Neon Storage: нет доступа. Проверьте ключи и права storage:read/storage:write"
    elif status == 404:
        message = "Neon Storage: bucket не найден. Проверьте STORAGE_BUCKET и выбранную ветку проекта"
    else:
        message = "Neon Storage временно недоступен. Проверьте настройки и лимиты; повторите позже"
    raise RuntimeError(message) from None


def check_bucket():
    if not enabled():
        return
    try:
        with closing(_client()) as client:
            client.head_bucket(Bucket=_bucket())
    except (BotoCoreError, ClientError) as exc:
        _error(exc)


def put(name, path, *, upsert=False):
    if not enabled():
        return
    _name(name)
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise ValueError("Максимальный размер одного фото или видео — 50 МБ")
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as source, closing(_client()) as client:
            # New product uploads use a fresh UUID. The optional migration may
            # deliberately overwrite the same saved UUID when retried.
            client.put_object(Bucket=_bucket(), Key=name, Body=source,
                              ContentType=content_type)
    except (BotoCoreError, ClientError) as exc:
        _error(exc)


def ensure_local(name, directory):
    """Restore a missing cache file atomically; never return a partial download."""
    _name(name)
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, name)
    if os.path.isfile(dest):
        return dest
    if not enabled():
        raise FileNotFoundError(dest)
    fd, tmp = tempfile.mkstemp(prefix=".download-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as target, closing(_client()) as client:
            response = client.get_object(Bucket=_bucket(), Key=name)
            size = 0
            with closing(response["Body"]) as body:
                while chunk := body.read(512 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise RuntimeError("Файл в Storage превышает лимит приложения 50 МБ")
                    target.write(chunk)
        os.replace(tmp, dest)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "NotFound", "404"):
            raise FileNotFoundError(dest) from None
        _error(exc)
    except BotoCoreError as exc:
        _error(exc)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dest


def delete(name):
    if not enabled():
        return
    _name(name)
    try:
        with closing(_client()) as client:
            client.delete_object(Bucket=_bucket(), Key=name)
    except (BotoCoreError, ClientError):
        # The DB deletion is already committed. A leftover object is safer than
        # reporting a failed product deletion; cleanup can be retried later.
        log.warning("Не удалось удалить медиа из Neon Storage; повторите очистку позже")
