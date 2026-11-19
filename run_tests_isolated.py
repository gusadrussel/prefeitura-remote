"""Run unittest with disposable persistence and agent configuration only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="prefeitura-tests-") as directory:
        temporary = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("PREFEITURA_")}
        env.update({
            "PREFEITURA_DATABASE_URL": f"sqlite:///{temporary / 'bootstrap.db'}",
            "PREFEITURA_AGENT_ID_FILE": str(temporary / "agent_id"),
            "PROGRAMDATA": str(temporary),
            "XDG_CONFIG_HOME": str(temporary),
            "XDG_STATE_HOME": str(temporary),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temporary), "TMP": str(temporary),
        })
        args = sys.argv[1:] or ["discover", "-v"]
        try:
            return subprocess.run([sys.executable, "-B", "-m", "unittest", *args],
                                  cwd=root, env=env, timeout=180).returncode
        except subprocess.TimeoutExpired:
            print("Test process exceeded 180 seconds and was stopped.", file=sys.stderr)
            return 124


if __name__ == "__main__":
    raise SystemExit(main())
