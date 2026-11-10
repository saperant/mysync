"""Backend contract tests.

Local and rclone (driving a local directory) run whenever possible.  The SSH
based backends need passwordless access to a host:

    MYSYNC_TEST_SSH_HOST=localhost pytest

The rclone tests use ``MYSYNC_TEST_RCLONE`` or ``rclone`` from PATH.
"""

from __future__ import annotations

import io
import os
import shutil

import pytest

from mysync.backends import open_backend
from mysync.config import job_from_dict
from mysync.model import DIR, FILE, LINK, Action

from conftest import Pair, tree, write

SSH_HOST = os.environ.get("MYSYNC_TEST_SSH_HOST")
RCLONE = os.environ.get("MYSYNC_TEST_RCLONE") or shutil.which("rclone")


def remote_location(kind: str, path, tmp_path) -> dict:
    if kind == "local":
        return {"url": str(path)}
    if kind == "rclone":
        if not RCLONE:
            pytest.skip("rclone not available")
        return {"url": f"rclone:{path}", "rclone": RCLONE, "rclone_flags": ["--config", "/dev/null"]}
    if not SSH_HOST:
        pytest.skip("set MYSYNC_TEST_SSH_HOST to test ssh/sftp")
    return {"url": f"{kind}://{SSH_HOST}{path}", "host_key_policy": "accept-new",
            "known_hosts": str(tmp_path / "known_hosts")}


KINDS = ["local", "sftp", "ssh", "rclone"]


@pytest.fixture(params=KINDS)
def backend(request, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    job = job_from_dict({"left": remote_location(request.param, root, tmp_path),
                         "right": str(tmp_path / "other")}, "t")
    with open_backend(job.left) as b:
        yield b, root


def test_contract(backend, past):
    b, root = backend
    assert b.root_exists()
    write(root / "a.txt", "hello", past)
    write(root / "sub/deep/b.bin", "x" * 3000, past + 1.5)
    # rclone presents control characters in names with its own encoding
    odd = "weird name ü.txt" if b.loc.scheme == "rclone" else "weird name\twith tab.txt"
    write(root / odd, "t", past)
    os.symlink("a.txt", root / "link")
    scan = b.scan(lambda rel, is_dir: rel == "sub/deep", "skip")
    assert set(scan) == {"a.txt", "sub", odd}
    assert scan["a.txt"].kind == FILE and scan["a.txt"].size == 5
    assert abs(scan["a.txt"].mtime - past) < 1
    assert scan["sub"].kind == DIR

    with b.open_read("a.txt") as f:
        assert f.read() == b"hello"
    with pytest.raises(FileNotFoundError):
        with b.open_read("missing.txt") as f:
            f.read()

    entry = b.write_file("sub/new.txt", io.BytesIO(b"new content"), past + 7)
    assert entry.size == 11 and abs(entry.mtime - (past + 7)) < 1
    assert (root / "sub/new.txt").read_text() == "new content"
    assert abs(os.stat(root / "sub/new.txt").st_mtime - (past + 7)) < 1
    entry = b.write_file("sub/new.txt", io.BytesIO(b"replaced"), past + 9)  # overwrite
    assert (root / "sub/new.txt").read_text() == "replaced"
    assert not [p for p in root.rglob("*") if p.name.endswith(".mysync-tmp")]

    assert b.mkdir("x/y/z").kind == DIR
    assert (root / "x/y/z").is_dir()
    b.mkdir("x/y/z")  # exists already: fine

    b.move("a.txt", "trash/2024/a.txt")
    assert (root / "trash/2024/a.txt").read_text() == "hello"
    assert sorted(b.list_names("trash")) == ["2024"]
    assert b.list_names("nope") == []

    b.remove("trash/2024/a.txt", False)
    assert not (root / "trash/2024/a.txt").exists()
    b.remove("x", True)
    assert not (root / "x").exists()

    import hashlib
    assert b.hash_file("sub/new.txt") == hashlib.sha256(b"replaced").hexdigest()
    algos = b.hash_algos()
    if b.loc.scheme in ("local", "ssh", "sftp", "rclone"):  # all can hash where the data is here
        assert {"sha256", "md5"} <= algos, algos
    for algo in ("md5", "sha256"):
        got = b.hash_many(["sub/new.txt", odd, "missing"], algo)
        assert got == {"sub/new.txt": hashlib.new(algo, b"replaced").hexdigest(),
                       odd: hashlib.new(algo, b"t").hexdigest()}

    e = b.set_attrs("sub/new.txt", past + 42, None if b.loc.scheme == "rclone" else 0o640)
    assert abs(e.mtime - (past + 42)) < 1
    assert abs(os.stat(root / "sub/new.txt").st_mtime - (past + 42)) < 1
    if b.loc.scheme != "rclone":
        assert os.stat(root / "sub/new.txt").st_mode & 0o777 == 0o640
        assert b.scan(lambda r, d: False, "skip")["sub/new.txt"].mode == 0o640

    write(root / "occupied.txt", "o", past)
    with pytest.raises(FileExistsError):
        b.move("sub/new.txt", "occupied.txt")
    assert (root / "occupied.txt").read_text() == "o"

    # stat for the change check before modifying
    st = b.stat("occupied.txt", "skip")
    assert st.kind == FILE and st.size == 1 and abs(st.mtime - past) < 1
    assert st.same_as(b.scan(lambda r, d: False, "skip")["occupied.txt"])
    assert b.stat("sub", "skip").kind == DIR
    assert b.stat("nope.txt", "skip") is None
    if b.loc.scheme != "rclone":  # rclone stat follows links of local folders
        assert b.stat("link", "skip") is None


def test_links(backend, past):
    b, root = backend
    if b.loc.scheme == "rclone":
        pytest.skip("rclone remotes have no symbolic links")
    write(root / "dir/target.txt", "t", past)
    os.symlink("dir/target.txt", root / "rel-link")
    scan = b.scan(lambda r, d: False, "links")
    assert scan["rel-link"].kind == LINK and scan["rel-link"].target == "dir/target.txt"
    assert b.scan(lambda r, d: False, "follow")["rel-link"].kind == FILE
    assert "rel-link" not in b.scan(lambda r, d: False, "skip")
    e = b.write_link("dir/new-link", "target.txt")
    assert e.kind == LINK and e.target == "target.txt"
    assert os.readlink(root / "dir/new-link") == "target.txt"
    b.write_link("dir/new-link", "../rel-link")  # replace
    assert os.readlink(root / "dir/new-link") == "../rel-link"
    assert b.stat("dir/new-link", "links").target == "../rel-link"


def test_delta_primitives(backend):
    from mysync.backends.base import block_digest, block_hashes_of
    b, root = backend
    if not b.supports_delta():
        assert b.loc.scheme == "rclone"
        return
    data = bytes(range(256)) * 4096  # 1 MiB
    (root / "big.bin").write_bytes(data)
    bs = 65536
    hashes = b.block_hashes("big.bin", bs)
    import io
    assert hashes == block_hashes_of(io.BytesIO(data), bs) and len(hashes) == 16
    got = list(b.read_blocks("big.bin", [(0, 10), (70000, 5), (len(data) - 3, 3)]))
    assert got == [(0, data[:10]), (70000, data[70000:70005]), (len(data) - 3, data[-3:])]
    new = bytearray(data)
    new[100000:100010] = b"X" * 10
    new = bytes(new) + b"tail"
    want = block_hashes_of(io.BytesIO(new), bs)
    changed = [i for i, h in enumerate(want) if i >= len(hashes) or hashes[i] != h]
    blocks = [(i * bs, new[i * bs:(i + 1) * bs]) for i in changed]
    e = b.patch_file("big.bin", blocks, len(new), 1700000000.0, None, "old/big.bin",
                     (bs, block_digest(want)), None)
    assert (root / "big.bin").read_bytes() == new and e.size == len(new)
    assert (root / "old/big.bin").read_bytes() == data
    assert abs(os.stat(root / "big.bin").st_mtime - 1700000000) < 1
    # a wrong expectation must leave the file untouched
    with pytest.raises(OSError):
        b.patch_file("big.bin", [(0, b"garbage")], len(new), 1.0, None, None, (bs, "0" * 64), None)
    assert (root / "big.bin").read_bytes() == new
    assert not [p for p in root.rglob("*") if p.name.endswith(".mysync-tmp")]

    assert b.read_blob("state/s.gz") is None
    b.write_blob("state/s.gz", b"\x00\x01blob")
    assert b.read_blob("state/s.gz") == b"\x00\x01blob"


def test_missing_root(tmp_path):
    for kind in KINDS:
        try:
            loc = remote_location(kind, tmp_path / "does-not-exist", tmp_path)
        except pytest.skip.Exception:
            continue
        job = job_from_dict({"left": loc, "right": str(tmp_path / "o")}, "t")
        with open_backend(job.left) as b:
            assert not b.root_exists(), kind
            assert b.scan(lambda r, d: False, "skip") == {}, kind


@pytest.mark.parametrize("kind", KINDS)
def test_two_way_sync_with_remote(kind, tmp_path, past):
    rloc = remote_location(kind, tmp_path / "right", tmp_path)
    rloc["state_dir"] = ".mysync"  # keep this side's state on the remote itself
    p = Pair(tmp_path, right_url=None, deletion="trash")
    p.job = job_from_dict({"left": str(p.left), "right": rloc, "deletion": "trash"}, "t")

    write(p.left / "docs/a.txt", "A", past)
    write(p.left / "b.txt", "B", past)
    write(p.right / "c.txt", "C", past)
    _, res = p.sync()
    assert res.failed == 0, res.errors
    expected = {"b.txt": "B", "c.txt": "C", "docs": "/", "docs/a.txt": "A"}
    assert tree(p.left) == tree(p.right) == expected
    assert p.compare().pending() == []  # times survived the round trip

    (p.right / "b.txt").unlink()
    write(p.left / "docs/a.txt", "A2", past + 60)
    cmp = p.compare()
    assert cmp.by_path["b.txt"].action == Action.DELETE_L
    assert cmp.by_path["docs/a.txt"].action == Action.COPY_LR
    _, res = p.sync()
    assert res.failed == 0, res.errors
    assert tree(p.left) == tree(p.right) == {"c.txt": "C", "docs": "/", "docs/a.txt": "A2"}
    assert [t.read_text() for t in (p.right / ".mysync/trash").rglob("a.txt")] == ["A"]
    assert p.compare().pending() == []

    # renames and folder renames on either side are replayed, not transferred
    (p.left / "docs").rename(p.left / "documents")
    (p.right / "c.txt").rename(p.right / "c-renamed.txt")
    cmp = p.compare()
    assert cmp.by_path["documents"].action == Action.MOVE_R
    assert cmp.by_path["c-renamed.txt"].action == Action.MOVE_L
    _, res = p.sync()
    assert res.failed == 0 and res.bytes == 0, res.errors
    assert tree(p.left) == tree(p.right) == {"c-renamed.txt": "C", "documents": "/", "documents/a.txt": "A2"}
    assert p.compare().pending() == []

    # only the modification time changed: verified by checksums, no transfer
    os.utime(p.left / "documents/a.txt", (past + 3600, past + 3600))
    cmp = p.compare()
    assert cmp.by_path["documents/a.txt"].action == Action.ATTR_LR, cmp.by_path["documents/a.txt"]
    _, res = p.sync()
    assert res.failed == 0 and res.bytes == 0, res.errors
    assert abs(os.stat(p.right / "documents/a.txt").st_mtime - (past + 3600)) < 1
    assert p.compare().pending() == []


@pytest.mark.parametrize("kind", KINDS)
def test_delta_transfer(kind, tmp_path, past):
    rloc = remote_location(kind, tmp_path / "right", tmp_path)
    p = Pair(tmp_path)
    p.job = job_from_dict({"left": str(p.left), "right": rloc, "delta_min_size": "1M"}, "t")
    data = bytearray(os.urandom(6 * 1024 * 1024))
    (p.left / "disk.img").write_bytes(data)
    _, res = p.sync()
    assert res.bytes == len(data)
    data[3_000_000:3_000_100] = b"\xff" * 100  # small edit in the middle
    (p.left / "disk.img").write_bytes(data)
    os.utime(p.left / "disk.img", (past, past))
    _, res = p.sync()
    assert res.failed == 0, res.errors
    assert (p.right / "disk.img").read_bytes() == bytes(data)
    if kind in ("ssh", "sftp"):
        assert res.bytes < 200_000, res.bytes  # one block instead of 6 MiB
        assert [t.stat().st_size for t in (p.right / ".mysync/trash").rglob("disk.img")] == [len(data)]
    else:
        assert res.bytes == len(data)  # local<->local and cloud: whole file
    # changed on the target too (both sides edited): the result must still be exact
    data[0:10] = b"0123456789"
    (p.left / "disk.img").write_bytes(data)
    _, res = p.sync()
    assert (p.right / "disk.img").read_bytes() == bytes(data)


@pytest.mark.parametrize("kind", KINDS)
def test_selective_free_with_remote(kind, tmp_path, past):
    from mysync import selection
    rloc = remote_location(kind, tmp_path / "right", tmp_path)
    p = Pair(tmp_path)
    p.job = job_from_dict({"left": str(p.left), "right": rloc, "selective": "left", "retries": 0}, "t")
    write(p.left / "Old/a.bin", "a" * 3000, past)
    write(p.left / "Old/sub/b.txt", "b", past)
    write(p.left / "Keep/k.txt", "k", past)
    write(p.right / "Server/s.txt", "s", past)
    sel = selection.Selection()
    sel.set("Server", False)
    selection.save(p.job, sel)
    _, res = p.sync()
    assert res.failed == 0, res.errors
    assert not (p.left / "Server").exists()

    write(p.left / "Old/new.txt", "made locally", past)
    write(p.right / "Old/a.bin", "x" * 3000, past)  # corrupt copy: same size and time
    sel.set("Old", False)
    selection.save(p.job, sel)
    cmp, res = p.sync()
    assert cmp.by_path["Old/a.bin"].status == "failed", kind  # checksums on the server caught it
    assert (p.left / "Old/a.bin").read_text() == "a" * 3000
    assert not (p.left / "Old/sub").exists() and not (p.left / "Old/new.txt").exists()
    assert (p.right / "Old/new.txt").read_text() == "made locally"
    assert cmp.by_path["Old"].status == "kept"
    assert tree(p.right)["Server/s.txt"] == "s"


def test_block_hash_scripts_agree(tmp_path):
    import shlex
    import subprocess
    from mysync.backends.base import posix_block_hash_script
    f = tmp_path / "data.bin"
    for size in (0, 1, 65536, 65536 * 3 + 17):
        f.write_bytes(os.urandom(size))
        out = [subprocess.run(["sh", "-c", posix_block_hash_script(shlex.quote(str(f)), 65536, gnu)],
                              capture_output=True, check=True).stdout for gnu in (True, False)]
        assert out[0] == out[1], size


def test_hashes_of_awkward_names(backend):
    """Remote checksum tools escape some names in their output (FreeBSD:
    "ä" -> "\\303\\244"); results must still be matched to the right files."""
    import hashlib
    b, root = backend
    names = ["Raportti 1.2.03.doc", "Ääni ja kuva.txt", "back\\slash.txt", "two  spaces.txt", "dir ö/x.jpg"]
    if b.loc.scheme != "rclone":
        names.append("new\nline.txt")
    for i, name in enumerate(names):
        write(root / name, f"content {i}" * (i + 1))
    for algo in sorted(b.hash_algos() & {"sha256", "md5"}):
        expected = {n: hashlib.new(algo, (root / n).read_bytes()).hexdigest() for n in names}
        assert b.hash_many(names + ["missing.txt", "dir ö"], algo) == expected, algo
