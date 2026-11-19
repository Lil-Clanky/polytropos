import json
import re
from pathlib import Path

import pytest
import yaml

import check_updates as cu
import release

ROOT = Path(__file__).resolve().parent.parent
SHA_A = "a" * 40
SHA_B = "b" * 40
SECRET = re.compile(r"(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key|credential|auth)")

PACK = f"""# comment kept
name: polytropos
plugins:
  - repo: Lil-Clanky/polytropos
    ref: {SHA_A}
    subdir: plugins/polytropos
  - repo: other/thing
    ref: {SHA_A}
  - repo: Lil-Clanky/polytropos
    ref: {SHA_A}  # trailing
    subdir: plugins/polytropos_router
"""


def test_bump_pack_refs_rewrites_only_matching_entries(tmp_path):
    p = tmp_path / "pack.yaml"
    p.write_text(PACK)
    assert release.bump_pack_refs(p, "Lil-Clanky/polytropos", SHA_B) == 2
    lines = p.read_text().splitlines()
    assert lines[0] == "# comment kept"
    refs = [l.strip() for l in lines if l.strip().startswith("ref:")]
    assert refs == [f"ref: {SHA_B}", f"ref: {SHA_A}", f"ref: {SHA_B}  # trailing"]


@pytest.mark.parametrize("bad", ["abc", "g" * 40, "A" * 40, SHA_A + "0"])
def test_bump_pack_refs_rejects_bad_sha(tmp_path, bad):
    p = tmp_path / "pack.yaml"
    p.write_text(PACK)
    with pytest.raises(ValueError):
        release.bump_pack_refs(p, "Lil-Clanky/polytropos", bad)
    assert p.read_text() == PACK


def test_newer_compares_numbers_not_text():
    assert cu.newer("v0.16.36", "v0.16.9")
    assert not cu.newer("v0.16.9", "v0.16.36")
    assert not cu.newer("1.2.1", "v1.2.1")
    assert cu.parse_version("v7.0.2") == (7, 0, 2)


def test_update_lock_keeps_comments_and_other_sections(tmp_path):
    p = tmp_path / "versions.lock"
    p.write_text('# top\n[a]\n# why\ntag = "v1"  # note\ncommit = "x"\n\n[b]\ntag = "v1"\n')
    assert cu.update_lock(p, "a", {"tag": "v2", "commit": "y"})
    assert p.read_text() == '# top\n[a]\n# why\ntag = "v2"  # note\ncommit = "y"\n\n[b]\ntag = "v1"\n'
    assert not cu.update_lock(p, "a", {"tag": "v2"})
    assert not cu.update_lock(p, "missing", {"tag": "v3"})


def test_update_lock_handles_dotted_sections(tmp_path):
    p = tmp_path / "versions.lock"
    p.write_text('[rtk]\nversion = "1"\n[rtk.assets.darwin-arm64]\nsha256 = "x"\n')
    assert cu.update_lock(p, "rtk.assets.darwin-arm64", {"sha256": "z"})
    assert p.read_text() == '[rtk]\nversion = "1"\n[rtk.assets.darwin-arm64]\nsha256 = "z"\n'


def fake_fetch(table):
    def fetch(url):
        if url not in table:
            raise OSError(url)
        return json.dumps(table[url]).encode()
    return fetch


def test_latest_tag_uses_release_then_tags():
    api = "https://api.github.com/repos/o/r"
    assert cu.latest_tag("o/r", fake_fetch({f"{api}/releases/latest": {"tag_name": "v2.0.0"}})) == "v2.0.0"
    tags = [{"name": "v1.9.0"}, {"name": "v1.10.0"}, {"name": "nightly"}]
    assert cu.latest_tag("o/r", fake_fetch({f"{api}/tags": tags})) == "v1.10.0"


def test_find_updates_reports_only_newer(tmp_path):
    lock = tmp_path / "versions.lock"
    lock.write_text(
        '[freellmapi]\ntag = "v0.16.36"\ncli_version = "0.8.2"\n[rtk]\nversion = "1.2.1"\n'
        '[graphify]\npackage = "graphifyy"\nversion = "0.9.112"\n'
        '[ast-grep]\npackage = "ast-grep-cli"\nversion = "0.51.3"\n'
        '[plugins.ponytail]\ntag = "v5.3.3"\n[plugins.superpowers]\ntag = "v7.0.2"\n'
    )
    gh = "https://api.github.com/repos/"
    fetch = fake_fetch({
        gh + "tashfeenahmed/freellmapi/releases/latest": {"tag_name": "v0.16.37"},
        "https://registry.npmjs.org/freellmapi/latest": {"version": "0.8.3"},
        gh + "rtk-ai/rtk/releases/latest": {"tag_name": "v1.2.1"},
        gh + "DietrichGebert/ponytail/releases/latest": {"tag_name": "v5.3.3"},
        gh + "obra/superpowers/releases/latest": {"tag_name": "v7.1.0"},
        "https://pypi.org/pypi/graphifyy/json": {"info": {"version": "0.9.113"}},
        "https://pypi.org/pypi/ast-grep-cli/json": {"info": {"version": "0.51.3"}},
    })
    assert cu.find_updates(lock, fetch) == [
        {"component": "freellmapi", "current": "v0.16.36", "latest": "v0.16.37"},
        {"component": "freellmapi-cli", "current": "0.8.2", "latest": "0.8.3"},
        {"component": "graphify", "current": "0.9.112", "latest": "0.9.113"},
        {"component": "superpowers", "current": "v7.0.2", "latest": "v7.1.0"},
    ]


def test_pack_file_pins_and_config():
    pack = yaml.safe_load((ROOT / "hermes-pack.yaml").read_text())
    plugins = pack["plugins"]
    assert [(p["repo"], p.get("subdir")) for p in plugins] == [
        ("Lil-Clanky/polytropos", "plugins/polytropos"),
        ("Lil-Clanky/polytropos", "plugins/polytropos_router"),
        ("DietrichGebert/ponytail", None),
        ("obra/superpowers", None),
    ]
    assert all(re.fullmatch(r"[0-9a-f]{40}", p["ref"]) for p in plugins)
    assert pack["config"] == {"polytropos-router": {"default_route": "complex"}}

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield str(k)
                yield from keys(v)
        elif isinstance(node, list):
            for v in node:
                yield from keys(v)

    for k in keys(pack["config"]):
        assert not SECRET.search(k), k
        assert k not in ("granted_capabilities", "capabilities_consent")
        assert not k.startswith("allow_"), k


def test_pack_third_party_refs_match_lock():
    import tomllib
    lock = tomllib.loads((ROOT / "versions.lock").read_text())["plugins"]
    pack = yaml.safe_load((ROOT / "hermes-pack.yaml").read_text())["plugins"]
    refs = {p["repo"]: p["ref"] for p in pack}
    assert refs["DietrichGebert/ponytail"] == lock["ponytail"]["commit"]
    assert refs["obra/superpowers"] == lock["superpowers"]["commit"]


def test_apply_update_writes_lock_and_pack(tmp_path):
    lock, pack = tmp_path / "versions.lock", tmp_path / "pack.yaml"
    lock.write_text('[plugins.ponytail]\ntag = "v1.0.0"\ncommit = "old"\n')
    pack.write_text(f"plugins:\n  - repo: DietrichGebert/ponytail\n    ref: {SHA_A}\n")
    fetch = fake_fetch({f"{cu.GITHUB}DietrichGebert/ponytail/commits/v1.1.0": {"sha": SHA_B}})
    cu.apply_update(lock, pack, {"component": "ponytail", "current": "v1.0.0", "latest": "v1.1.0"}, fetch)
    assert lock.read_text() == f'[plugins.ponytail]\ntag = "v1.1.0"\ncommit = "{SHA_B}"\n'
    assert SHA_B in pack.read_text()


def test_apply_freellmapi_cli_rewrites_only_cli_version(tmp_path):
    lock = tmp_path / "versions.lock"
    lock.write_text('[freellmapi]\ntag = "v1"\ncommit = "c"\ncli_version = "0.8.2"\n')
    cu.apply_update(lock, tmp_path / "none.yaml", {"component": "freellmapi-cli", "current": "0.8.2", "latest": "0.8.3"}, fake_fetch({}))
    assert lock.read_text() == '[freellmapi]\ntag = "v1"\ncommit = "c"\ncli_version = "0.8.3"\n'


def test_apply_rtk_keeps_version_without_v_and_reads_checksums(tmp_path):
    base = "https://github.com/rtk-ai/rtk/releases/download/"
    lock = tmp_path / "versions.lock"
    lock.write_text(
        '[rtk]\nversion = "1.2.1"\nrepo = "https://github.com/rtk-ai/rtk"\n\n'
        f'[rtk.assets.darwin-arm64]\nurl = "{base}v1.2.1/rtk-a.tar.gz"\nsha256 = "old1"\n\n'
        f'[rtk.assets.linux-x86_64]\nurl = "{base}v1.2.1/rtk-b.tar.gz"\nsha256 = "old2"\n'
    )
    sums = f"{'1' * 64}  rtk-a.tar.gz\n{'2' * 64} *rtk-b.tar.gz\n"
    fetch = lambda url: sums.encode() if url == f"{base}v1.2.2/checksums.txt" else (_ for _ in ()).throw(OSError(url))
    cu.apply_update(lock, tmp_path / "none.yaml", {"component": "rtk", "current": "1.2.1", "latest": "v1.2.2"}, fetch)
    import tomllib
    data = tomllib.loads(lock.read_text())["rtk"]
    assert data["version"] == "1.2.2"
    assert data["assets"]["darwin-arm64"] == {"url": f"{base}v1.2.2/rtk-a.tar.gz", "sha256": "1" * 64}
    assert data["assets"]["linux-x86_64"] == {"url": f"{base}v1.2.2/rtk-b.tar.gz", "sha256": "2" * 64}
