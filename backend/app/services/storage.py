from __future__ import annotations

import os
from typing import Optional, Tuple

from ..config import settings

try:
    import boto3  # type: ignore
    from botocore.client import Config as BotoConfig  # type: ignore
except Exception:  # noqa: BLE001
    boto3 = None  # type: ignore
    BotoConfig = None  # type: ignore


class StorageService:
    def __init__(self) -> None:
        self.backend = settings.storage_backend
        self.local_root = settings.storage_local_root
        if self.backend == "s3":
            if boto3 is None:
                raise RuntimeError("boto3 is required for S3 backend")
            session = boto3.session.Session(
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
            )
            self.s3 = session.resource(
                "s3",
                endpoint_url=settings.s3_endpoint,
                config=BotoConfig(s3={"addressing_style": "virtual"}),
            )
            self.bucket = self.s3.Bucket(settings.s3_bucket)  # type: ignore[arg-type]
        else:
            os.makedirs(self.local_root, exist_ok=True)

    def save_bytes(self, data: bytes, subdir: str, filename: str) -> Tuple[str, Optional[str]]:
        # returns (storage_path, public_url)
        if self.backend == "s3":
            key = f"{subdir}/{filename}"
            self.bucket.put_object(Key=key, Body=data)  # type: ignore[union-attr]
            # public URL (assumes public bucket or presign elsewhere)
            base = settings.s3_endpoint or f"https://{settings.s3_bucket}.s3.{settings.s3_region}.amazonaws.com"
            url = f"{base}/{key}"
            return key, url
        # local
        dest_dir = os.path.join(self.local_root, subdir)
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, filename)
        with open(path, "wb") as f:
            f.write(data)
        return path, None

    def load_bytes(self, subdir: str, filename: str) -> Optional[bytes]:
        if self.backend == "s3":
            try:
                key = f"{subdir}/{filename}"
                obj = self.bucket.Object(key)  # type: ignore[union-attr]
                body = obj.get()["Body"].read()
                return body
            except Exception:
                return None
        # local
        path = os.path.join(self.local_root, subdir, filename)
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            return None


storage = StorageService()


