#!/usr/bin/env python3
"""
obsidian-azure-sync — bidirectional sync between a local Obsidian vault
and an Azure Blob Storage container.

Authentication uses Azure AD (Entra ID) with device-code flow on first run;
the access token is cached in ~/.obsidian-sync/token_cache.bin so subsequent
runs are silent.

Usage
-----
    python sync.py                     # bidirectional sync (default)
    python sync.py --push              # local  → cloud only
    python sync.py --pull              # cloud  → local only
    python sync.py --dry-run           # show what would change, no writes
    python sync.py --config path.yaml  # use a custom config file
    python sync.py --delete            # also delete files removed on either side
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from azure.identity import (
    ChainedTokenCredential,
    ClientSecretCredential,
    DeviceCodeCredential,
    TokenCachePersistenceOptions,
)
from azure.storage.blob import BlobServiceClient, ContentSettings

# ──────────────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("obsidian-sync")

# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG_PATH = Path.home() / ".obsidian-sync" / "config.yaml"
TOKEN_CACHE_DIR = Path.home() / ".obsidian-sync"
IGNORE_PATTERNS: list[str] = [
    ".obsidian/workspace.json",  # per-machine UI state
    ".obsidian/workspace-mobile.json",
    ".DS_Store",
    "Thumbs.db",
    "*.tmp",
    "~$*",
]


@dataclass
class Config:
    tenant_id: str
    client_id: str
    storage_account_name: str
    container_name: str
    vault_path: str
    # Optional — if set, uses client credentials instead of device code
    client_secret: Optional[str] = None
    # Resolved at load time
    vault_dir: Path = field(init=False)

    def __post_init__(self) -> None:
        self.vault_dir = Path(self.vault_path).expanduser().resolve()
        if not self.vault_dir.exists():
            raise FileNotFoundError(f"Vault path does not exist: {self.vault_dir}")

    @classmethod
    def load(cls, path: Path) -> "Config":
        if not path.exists():
            raise FileNotFoundError(
                f"Config not found at {path}.\n"
                "Copy sync_client/config.example.yaml → ~/.obsidian-sync/config.yaml "
                "and fill in your values."
            )
        raw = yaml.safe_load(path.read_text())
        # Allow environment variable overrides (useful in CI)
        raw["client_secret"] = raw.get("client_secret") or os.getenv(
            "OBSIDIAN_SYNC_CLIENT_SECRET"
        )
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


# ──────────────────────────────────────────────────────────────────────────────
# Authentication
# ──────────────────────────────────────────────────────────────────────────────


def build_credential(cfg: Config):
    """
    Return an Azure credential.
    - If client_secret is configured → ClientSecretCredential (headless / CI).
    - Otherwise → DeviceCodeCredential with persistent token cache.
    """
    TOKEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    if cfg.client_secret:
        log.debug("Using client-secret credential (headless mode).")
        return ClientSecretCredential(
            tenant_id=cfg.tenant_id,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
        )

    cache_opts = TokenCachePersistenceOptions(
        name="obsidian-sync",
        allow_unencrypted_storage=False,  # Falls back gracefully on Linux
    )
    log.debug("Using device-code credential with persistent token cache.")
    return DeviceCodeCredential(
        tenant_id=cfg.tenant_id,
        client_id=cfg.client_id,
        cache_persistence_options=cache_opts,
    )


# ──────────────────────────────────────────────────────────────────────────────
# File utilities
# ──────────────────────────────────────────────────────────────────────────────


def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def should_ignore(rel_path: str) -> bool:
    from fnmatch import fnmatch

    name = Path(rel_path).name
    for pattern in IGNORE_PATTERNS:
        if fnmatch(rel_path, pattern) or fnmatch(name, pattern):
            return True
    return False


def local_files(vault_dir: Path) -> dict[str, Path]:
    """Return {relative_posix_path: absolute_path} for all files in vault."""
    result: dict[str, Path] = {}
    for p in vault_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(vault_dir).as_posix()
            if not should_ignore(rel):
                result[rel] = p
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Blob helpers
# ──────────────────────────────────────────────────────────────────────────────


def blob_service_client(cfg: Config, credential) -> BlobServiceClient:
    account_url = f"https://{cfg.storage_account_name}.blob.core.windows.net"
    return BlobServiceClient(account_url=account_url, credential=credential)


def list_blobs(container_client) -> dict[str, dict]:
    """Return {blob_name: {size, last_modified, content_md5}} for all blobs."""
    result: dict[str, dict] = {}
    for blob in container_client.list_blobs(include=["metadata"]):
        result[blob.name] = {
            "size": blob.size,
            "last_modified": blob.last_modified,
            "content_md5": blob.content_settings.content_md5 if blob.content_settings else None,
        }
    return result


def upload_file(container_client, blob_name: str, local_path: Path, dry_run: bool) -> None:
    log.info("  UPLOAD  %s", blob_name)
    if dry_run:
        return
    md5 = hashlib.md5(local_path.read_bytes()).digest()
    import base64
    md5_b64 = base64.b64encode(md5).decode()
    with local_path.open("rb") as data:
        container_client.upload_blob(
            name=blob_name,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_md5=md5_b64),
        )


def download_file(container_client, blob_name: str, local_path: Path, dry_run: bool) -> None:
    log.info("  DOWNLOAD  %s", blob_name)
    if dry_run:
        return
    local_path.parent.mkdir(parents=True, exist_ok=True)
    blob_client = container_client.get_blob_client(blob_name)
    with local_path.open("wb") as f:
        data = blob_client.download_blob()
        data.readinto(f)
    # Set mtime to blob's last_modified so next run can compare timestamps
    props = blob_client.get_blob_properties()
    mtime = props.last_modified.timestamp()
    os.utime(local_path, (mtime, mtime))


def delete_blob(container_client, blob_name: str, dry_run: bool) -> None:
    log.info("  DELETE_BLOB  %s", blob_name)
    if dry_run:
        return
    container_client.delete_blob(blob_name)


def delete_local(local_path: Path, dry_run: bool) -> None:
    log.info("  DELETE_LOCAL  %s", local_path)
    if dry_run:
        return
    local_path.unlink(missing_ok=True)
    # Remove empty parent directories
    try:
        local_path.parent.rmdir()
    except OSError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Conflict resolution
# ──────────────────────────────────────────────────────────────────────────────


def local_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def resolve_conflict(
    blob_name: str,
    local_path: Path,
    blob_meta: dict,
) -> str:
    """
    Returns 'upload' or 'download'.
    Strategy: last-writer-wins based on modification timestamps.
    """
    local_mt = local_mtime_utc(local_path)
    blob_mt: datetime = blob_meta["last_modified"]
    if blob_mt.tzinfo is None:
        blob_mt = blob_mt.replace(tzinfo=timezone.utc)

    delta = abs((local_mt - blob_mt).total_seconds())
    if delta < 2:
        # Within 2 s — treat as same (avoids thrashing on FAT/exFAT)
        return "skip"

    winner = "upload" if local_mt > blob_mt else "download"
    log.warning(
        "  CONFLICT  %s  local=%s  blob=%s  → %s",
        blob_name,
        local_mt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        blob_mt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        winner,
    )
    return winner


# ──────────────────────────────────────────────────────────────────────────────
# Sync logic
# ──────────────────────────────────────────────────────────────────────────────


def sync(
    cfg: Config,
    credential,
    *,
    push_only: bool = False,
    pull_only: bool = False,
    delete: bool = False,
    dry_run: bool = False,
) -> None:
    bsc = blob_service_client(cfg, credential)
    cc = bsc.get_container_client(cfg.container_name)

    log.info("Listing local files in %s …", cfg.vault_dir)
    local = local_files(cfg.vault_dir)

    log.info("Listing blobs in %s/%s …", cfg.storage_account_name, cfg.container_name)
    blobs = list_blobs(cc)

    uploads = downloads = skips = conflicts = deletes = 0

    # ── Files that exist locally ──────────────────────────────────────────
    if not pull_only:
        for rel, abs_path in local.items():
            if rel in blobs:
                action = resolve_conflict(rel, abs_path, blobs[rel])
                if action == "upload":
                    upload_file(cc, rel, abs_path, dry_run)
                    uploads += 1
                elif action == "download":
                    download_file(cc, rel, abs_path, dry_run)
                    downloads += 1
                else:
                    skips += 1
                conflicts += (action in ("upload", "download"))
            else:
                upload_file(cc, rel, abs_path, dry_run)
                uploads += 1

    # ── Files that exist only in the cloud ───────────────────────────────
    if not push_only:
        for blob_name in blobs:
            if blob_name not in local:
                local_path = cfg.vault_dir / blob_name
                download_file(cc, blob_name, local_path, dry_run)
                downloads += 1

    # ── Deletions ─────────────────────────────────────────────────────────
    if delete:
        for blob_name in blobs:
            if blob_name not in local and not pull_only:
                delete_blob(cc, blob_name, dry_run)
                deletes += 1
        for rel in local:
            if rel not in blobs and not push_only:
                delete_local(cfg.vault_dir / rel, dry_run)
                deletes += 1

    label = " [DRY RUN]" if dry_run else ""
    log.info(
        "Done%s — uploaded: %d  downloaded: %d  skipped: %d  conflicts: %d  deleted: %d",
        label, uploads, downloads, skips, conflicts, deletes,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bidirectional Obsidian vault sync via Azure Blob Storage."
    )
    p.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG_PATH,
        help=f"Path to config YAML (default: {DEFAULT_CONFIG_PATH})"
    )
    direction = p.add_mutually_exclusive_group()
    direction.add_argument("--push", action="store_true", help="Upload only (local → cloud)")
    direction.add_argument("--pull", action="store_true", help="Download only (cloud → local)")
    p.add_argument("--delete", action="store_true",
                   help="Delete files on the destination that don't exist on the source")
    p.add_argument("--dry-run", action="store_true",
                   help="Show changes without writing anything")
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        cfg = Config.load(args.config)
    except FileNotFoundError as e:
        log.error("%s", e)
        sys.exit(1)

    credential = build_credential(cfg)

    # Warm up credential (triggers device-code prompt if not cached)
    try:
        token = credential.get_token("https://storage.azure.com/.default")
        log.debug("Token acquired, expires at %s", datetime.fromtimestamp(token.expires_on))
    except Exception as e:
        log.error("Authentication failed: %s", e)
        sys.exit(1)

    sync(
        cfg,
        credential,
        push_only=args.push,
        pull_only=args.pull,
        delete=args.delete,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
