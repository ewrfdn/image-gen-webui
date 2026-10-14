from pathlib import Path
from .models import uid

def write_atomic(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uid() + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)
