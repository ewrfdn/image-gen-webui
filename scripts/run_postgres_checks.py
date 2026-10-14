"""Run platform tests against an isolated temporary PostgreSQL container, without GPU access."""

import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
name = "image-gen-platform-verify-" + secrets.token_hex(4)
password = secrets.token_hex(24)
created = False
try:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--publish",
            "127.0.0.1::5432",
            "--tmpfs",
            "/var/lib/postgresql/data:rw",
            "--env",
            "POSTGRES_USER=image_gen",
            "--env",
            "POSTGRES_DB=platform_verify",
            "--env",
            "POSTGRES_PASSWORD=" + password,
            "postgres:17-alpine",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print("Unable to start isolated PostgreSQL:", result.stderr, file=sys.stderr)
        sys.exit(1)
    created = True
    deadline = time.monotonic() + 45
    while subprocess.run(
        [
            "docker",
            "exec",
            name,
            "pg_isready",
            "-U",
            "image_gen",
            "-d",
            "platform_verify",
        ],
        capture_output=True,
    ).returncode:
        if time.monotonic() > deadline:
            raise RuntimeError("Verification PostgreSQL did not become ready")
        time.sleep(0.5)
    mapping = subprocess.check_output(
        ["docker", "port", name, "5432/tcp"], text=True
    ).strip()
    port = int(mapping.rsplit(":", 1)[-1])
    env = os.environ.copy()
    env["TEST_DATABASE_URL"] = (
        f"postgresql+asyncpg://image_gen:{password}@127.0.0.1:{port}/platform_verify"
    )
    result = subprocess.run(
        ["uv", "run", "--python", "3.12", "--extra", "test", "pytest", "-q"],
        cwd=root / "server",
        env=env,
    )
    sys.exit(result.returncode)
finally:
    if created:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
