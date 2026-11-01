"""Version and build commit information."""

import os
import subprocess
from pathlib import Path

# Static fallback updated with each commit
BUILD_COMMIT = "a24f9dc"


def get_git_commit() -> str:
    """Return the 7-8 character git commit hash of the currently running application."""
    for env_var in ("GIT_COMMIT", "SOURCE_COMMIT", "COOLIFY_COMMIT", "COMMIT_SHA", "APP_VERSION"):
        val = os.environ.get(env_var)
        if val:
            return val[:8]
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=1.0,
        )
        c = out.decode("utf-8").strip()
        if c:
            return c
    except Exception:
        pass
    try:
        git_head = Path(__file__).resolve().parent.parent / ".git" / "HEAD"
        if git_head.exists():
            ref = git_head.read_text(encoding="utf-8").strip()
            if ref.startswith("ref:"):
                ref_path = Path(__file__).resolve().parent.parent / ".git" / ref.split("ref:")[1].strip()
                if ref_path.exists():
                    return ref_path.read_text(encoding="utf-8").strip()[:8]
            else:
                return ref[:8]
    except Exception:
        pass
    return BUILD_COMMIT
