"""Build the deployment artifact, with a paper-mode config forced into it.

The repo's working copy of ``config/trading_rules.yaml`` tracks whatever the
operator is currently running locally, and today that is live. An artifact that
carried a live config would arm every VM it was ever deployed to, including a
rebuilt one, and would do so as a side effect of packaging rather than as a
decision. ``project.yaml`` forbids exactly that.

So the artifact always ships paper. Arming is done on the VM, once, by the
operator, and ``deploy/startup.sh`` preserves ``config/`` across redeploys so
that decision survives without ever living in the tarball.

The forcing is verified after the fact, from the built tarball rather than from
the source tree, so a bug in the rewrite fails the build instead of shipping.

Usage::

    python deploy/package.py                   # writes solomon-trader-app.tar.gz
    python deploy/package.py --out /tmp/x.tar.gz
    python deploy/package.py --verify-only path/to/artifact.tar.gz
"""

from __future__ import annotations

import argparse
import sys
import tarfile
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_MEMBER = "config/trading_rules.yaml"
DEFAULT_ARTIFACT = ROOT / "solomon-trader-app.tar.gz"

#: Everything the VM needs. tests/ is included deliberately: run_project_tests()
#: runs the suite before every gated live launch, so an artifact without tests
#: cannot trade.
INCLUDE = ("src", "tests", "config", "deploy", "assets", "requirements.txt",
           "requirements.lock.txt", "pytest.ini", "README.md")

#: Never ship these, whatever else changes.
EXCLUDE_NAMES = {".env", ".venv", ".git", "__pycache__", ".pytest_cache", "logs"}
EXCLUDE_SUFFIXES = (".db", ".pyc")


class UnsafeArtifact(Exception):
    """Raised when a built artifact would arm trading on deployment."""


def force_paper_config(raw: str) -> str:
    """Return ``raw`` with ``trading.enabled`` false and ``trading.mode`` paper.

    Everything else -- symbol lists, risk caps, order and exit settings -- is
    preserved exactly. Only the two keys that decide whether real orders can be
    placed are rewritten.
    """
    data = yaml.safe_load(raw) or {}
    trading = data.setdefault("trading", {})
    trading["enabled"] = False
    trading["mode"] = "paper"
    return yaml.safe_dump(data, sort_keys=False)


def config_is_paper(raw: str) -> bool:
    data = yaml.safe_load(raw) or {}
    trading = data.get("trading", {})
    return trading.get("enabled") is False and trading.get("mode") == "paper"


def verify_artifact(artifact: Path) -> None:
    """Fail loudly if the built tarball would arm trading, or cannot trade at all.

    Raises :class:`UnsafeArtifact` rather than returning a status, so a caller
    that forgets to check still fails the build.
    """
    with tarfile.open(artifact, "r:gz") as tar:
        names = tar.getnames()

        try:
            member = tar.extractfile(CONFIG_MEMBER)
        except KeyError:
            member = None
        if member is None:
            raise UnsafeArtifact(f"{artifact.name} has no config at {CONFIG_MEMBER}")
        raw = member.read().decode("utf-8")
        if not config_is_paper(raw):
            raise UnsafeArtifact(
                f"{artifact.name} ships a live-mode {CONFIG_MEMBER}; "
                "the artifact must always be paper"
            )

        leaked = [n for n in names if Path(n).name == ".env" or n.endswith(".db")]
        if leaked:
            raise UnsafeArtifact(f"{artifact.name} contains secrets or state: {leaked}")

        if not any(n.startswith("tests/") for n in names):
            raise UnsafeArtifact(
                f"{artifact.name} ships no tests; run_project_tests() gates every "
                "live launch and would refuse on this artifact"
            )


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & EXCLUDE_NAMES:
        return True
    return path.name.endswith(EXCLUDE_SUFFIXES)


def build(out: Path = DEFAULT_ARTIFACT, root: Path = ROOT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        forced = Path(tmp) / "trading_rules.yaml"
        source = root / CONFIG_MEMBER
        forced.write_text(force_paper_config(source.read_text(encoding="utf-8")),
                          encoding="utf-8")

        with tarfile.open(out, "w:gz") as tar:
            for entry in INCLUDE:
                path = root / entry
                if not path.exists():
                    continue
                if path.is_file():
                    tar.add(path, arcname=entry)
                    continue
                for child in sorted(path.rglob("*")):
                    if child.is_dir() or _should_skip(child.relative_to(root)):
                        continue
                    arcname = str(child.relative_to(root)).replace("\\", "/")
                    if arcname == CONFIG_MEMBER:
                        tar.add(forced, arcname=CONFIG_MEMBER)
                    else:
                        tar.add(child, arcname=arcname)

    verify_artifact(out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--verify-only", type=Path, default=None)
    args = parser.parse_args(argv)

    try:
        if args.verify_only:
            verify_artifact(args.verify_only)
            print(f"OK: {args.verify_only} ships a paper config")
        else:
            built = build(args.out)
            print(f"OK: built {built} (config forced to paper)")
    except UnsafeArtifact as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
