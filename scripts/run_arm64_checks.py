"""Validate the exact ARM64 application image with an isolated ARM64 PostgreSQL."""

import secrets
import subprocess
import sys
import time

suffix = secrets.token_hex(4)
network = "image-gen-verify-net-" + suffix
database = "image-gen-verify-db-" + suffix
password = secrets.token_hex(24)
network_created = False
database_created = False
try:
    subprocess.run(
        ["docker", "network", "create", network], check=True, capture_output=True
    )
    network_created = True
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--platform",
            "linux/arm64",
            "--name",
            database,
            "--network",
            network,
            "--network-alias",
            "database",
            "--tmpfs",
            "/var/lib/postgresql/data:rw",
            "--env",
            "POSTGRES_USER=image_gen",
            "--env",
            "POSTGRES_DB=platform_verify",
            "--env",
            "POSTGRES_PASSWORD=" + password,
            "image-gen-postgres:17-arm64",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(
            "Unable to start ARM64 verification database:",
            result.stderr,
            file=sys.stderr,
        )
        sys.exit(1)
    database_created = True
    deadline = time.monotonic() + 90
    while subprocess.run(
        [
            "docker",
            "exec",
            database,
            "pg_isready",
            "-U",
            "image_gen",
            "-d",
            "platform_verify",
        ],
        capture_output=True,
    ).returncode:
        if time.monotonic() > deadline:
            raise RuntimeError("ARM64 PostgreSQL did not become ready")
        time.sleep(0.5)
    url = f"postgresql+asyncpg://image_gen:{password}@database/platform_verify"
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/arm64",
            "--network",
            network,
            "--user",
            "0",
            "--entrypoint",
            "sh",
            "--env",
            "TEST_DATABASE_URL=" + url,
            "image-gen-platform:0.2.0",
            "-c",
            "uv sync --frozen --extra test --no-install-project && python -m pytest -q",
        ]
    )
    sys.exit(result.returncode)
finally:
    if database_created:
        subprocess.run(["docker", "rm", "--force", database], capture_output=True)
    if network_created:
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
