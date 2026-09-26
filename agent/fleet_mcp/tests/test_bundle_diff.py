"""bundle_diff on temp dirs and on the real repo bundles."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.fleet_mcp.bundle_diff import bundle_diff
from agent.fleet_mcp.errors import PreconditionError


def write(root: Path, version: str, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = root / version / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())


def test_changed_added_removed(tmp_path):
    write(tmp_path, "v1", {"a.py": "x=1\ny=2\n", "same.txt": "s\n", "gone.txt": "g\n", "sub/b.cfg": "k: 1\n"})
    write(
        tmp_path,
        "v2",
        {"a.py": "x=1\ny=3\nz=4\n", "same.txt": "s\n", "new.txt": "n1\nn2\n", "sub/b.cfg": "k: 1\n"},
    )
    out = bundle_diff(tmp_path, "v1", "v2")
    assert out["from"] == "v1" and out["to"] == "v2"
    assert out["summary"] == "1 changed, 1 added, 1 removed files"
    files = {f["path"]: f for f in out["files"]}
    assert [f["path"] for f in out["files"]] == ["a.py", "gone.txt", "new.txt"]
    a = files["a.py"]
    assert (a["status"], a["lines_added"], a["lines_removed"]) == ("changed", 2, 1)
    assert "-y=2" in a["diff"] and "+y=3" in a["diff"] and "+z=4" in a["diff"] and "@@" in a["diff"]
    assert "+++" not in a["diff"] and "---" not in a["diff"]
    assert (files["new.txt"]["status"], files["new.txt"]["lines_added"]) == ("added", 2)
    assert (files["gone.txt"]["status"], files["gone.txt"]["lines_removed"]) == ("removed", 1)


def test_truncation_and_file_cap(tmp_path):
    write(tmp_path, "v1", {"big.txt": "", **{f"f{i}.txt": "a\n" for i in range(4)}})
    write(
        tmp_path,
        "v2",
        {"big.txt": "".join(f"l{i}\n" for i in range(100)), **{f"f{i}.txt": "b\n" for i in range(4)}},
    )
    out = bundle_diff(tmp_path, "v1", "v2", max_files=2, max_lines_per_file=10)
    assert len(out["files"]) == 2 and out["omitted_files"] == 3
    assert out["summary"].startswith("5 changed")
    big = out["files"][0]
    assert big["path"] == "big.txt" and big["lines_added"] == 100
    lines = big["diff"].split("\n")
    assert len(lines) == 11 and lines[-1] == f"... ({100 + 1 - 10} more lines)"


def test_binary_file_reported_without_diff(tmp_path):
    write(tmp_path, "v1", {"blob.bin": b"\x00\x01\x02"})
    write(tmp_path, "v2", {"blob.bin": b"\x00\x01\x03", "bad.txt": b"\xff\xfe"})
    out = bundle_diff(tmp_path, "v1", "v2")
    by = {f["path"]: f for f in out["files"]}
    assert by["blob.bin"]["status"] == "changed" and by["blob.bin"]["diff"] == ""
    assert by["bad.txt"]["status"] == "added" and by["bad.txt"]["diff"] == ""


def test_identical_bundles(tmp_path):
    write(tmp_path, "v1", {"a": "1"})
    write(tmp_path, "v2", {"a": "1"})
    out = bundle_diff(tmp_path, "v1", "v2")
    assert out["files"] == [] and out["summary"] == "0 changed, 0 added, 0 removed files"


def test_unknown_version(tmp_path):
    write(tmp_path, "v1", {"a": "1"})
    for a, b in (("v1", "v9"), ("v9", "v1")):
        with pytest.raises(PreconditionError) as exc:
            bundle_diff(tmp_path, a, b)
        assert exc.value.code == "UNKNOWN_VERSION"


def test_path_traversal_rejected(tmp_path):
    write(tmp_path, "v1", {"a": "1"})
    with pytest.raises(PreconditionError) as exc:
        bundle_diff(tmp_path, "v1", "../v1")
    assert exc.value.code == "BAD_ARGUMENT"


BUNDLES = Path(__file__).resolve().parents[3] / "sim" / "bundles"


@pytest.mark.skipif(
    not (BUNDLES / "v1.1").is_dir() or not (BUNDLES / "v1.2").is_dir(), reason="no sim/bundles"
)
def test_real_bundles_v11_to_v12():
    out = bundle_diff(BUNDLES, "v1.1", "v1.2")
    paths = [f["path"] for f in out["files"]]
    assert "app/inference_app.py" in paths
    entry = next(f for f in out["files"] if f["path"] == "app/inference_app.py")
    assert entry["status"] == "changed" and entry["lines_added"] + entry["lines_removed"] > 0
