"""Tests for library_bundle.py — zip-based import/export of samples, kits, projects, MIDI."""

import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from mendell import library as library_mod
from mendell import library_bundle as bundle_mod


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_wav(path: Path, seconds: float = 0.1, sr: int = 22050) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.zeros(int(seconds * sr), dtype="float32"), sr)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def src_config(tmp_path, monkeypatch):
    """Source machine: isolated DB + config dir."""
    db_path = tmp_path / "src" / "library.db"
    db_path.parent.mkdir(parents=True)
    monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(db_path))
    return db_path


@pytest.fixture
def src_sample_folder(tmp_path) -> Path:
    """A tiny fake sample library with real WAV files."""
    folder = tmp_path / "my-samples"
    _write_wav(folder / "Kicks" / "kick_808.wav")
    _write_wav(folder / "Snares" / "snare_tight.wav")
    _write_wav(folder / "Loops" / "loop-120bpm.wav", seconds=1.0)
    return folder


@pytest.fixture
def dst_config(tmp_path, monkeypatch):
    """Destination machine: second isolated DB + config dir (no existing data)."""
    db_path = tmp_path / "dst" / "library.db"
    db_path.parent.mkdir(parents=True)
    return db_path


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

class TestExportBundle:
    def test_creates_zip_with_manifest(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder), tags=["drums"])
        out_zip = tmp_path / "bundle.zip"
        manifest = bundle_mod.export_bundle(out_zip, include=["samples"])
        assert out_zip.is_file()
        assert zipfile.is_zipfile(out_zip)
        assert manifest["schema_version"] == "1"
        assert "created_at" in manifest
        assert len(manifest["samples"]) == 1

    def test_manifest_json_inside_zip(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder))
        out_zip = tmp_path / "bundle.zip"
        bundle_mod.export_bundle(out_zip)
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
        assert "manifest.json" in names

    def test_sample_files_inside_zip(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder))
        out_zip = tmp_path / "bundle.zip"
        bundle_mod.export_bundle(out_zip, include=["samples"])
        with zipfile.ZipFile(out_zip, "r") as zf:
            names = zf.namelist()
        # all three WAV files should be present
        assert any("kick_808.wav" in n for n in names)
        assert any("snare_tight.wav" in n for n in names)
        assert any("loop-120bpm.wav" in n for n in names)

    def test_empty_library_produces_valid_bundle(self, src_config, tmp_path):
        """Bundle with nothing registered still produces a valid zip + manifest."""
        out_zip = tmp_path / "empty.zip"
        manifest = bundle_mod.export_bundle(out_zip)
        assert out_zip.is_file()
        assert manifest["samples"] == []
        assert manifest["kits"] == []

    def test_include_filter_excludes_other_types(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder))
        out_zip = tmp_path / "bundle.zip"
        manifest = bundle_mod.export_bundle(out_zip, include=["samples"])
        # kits/projects/midi lists should be empty (no data) and only samples included
        assert "samples" in manifest["include"]
        assert "kits" not in manifest["include"]

    def test_invalid_include_raises(self, src_config, tmp_path):
        with pytest.raises(ValueError, match="Unknown include value"):
            bundle_mod.export_bundle(tmp_path / "x.zip", include=["bogus"])

    def test_manifest_file_metadata(self, src_config, src_sample_folder, tmp_path):
        """Each sample file entry in the manifest carries at least rel_path and category."""
        library_mod.add("my-samples", str(src_sample_folder))
        out_zip = tmp_path / "bundle.zip"
        manifest = bundle_mod.export_bundle(out_zip, include=["samples"])
        lib = manifest["samples"][0]
        assert lib["name"] == "my-samples"
        assert lib["tags"] == []  # no tags added
        for f in lib["files"]:
            assert "rel_path" in f
            assert "category" in f

    def test_counts_in_returned_manifest(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder), tags=["drums"])
        out_zip = tmp_path / "bundle.zip"
        manifest = bundle_mod.export_bundle(out_zip, include=["samples"])
        counts = manifest.get("counts", {})
        assert counts.get("samples_libraries") == 1
        assert counts.get("samples_files") == 3


class TestImportBundle:
    def _make_bundle(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("my-samples", str(src_sample_folder), tags=["drums"])
        out_zip = tmp_path / "bundle.zip"
        bundle_mod.export_bundle(out_zip, include=["samples"])
        return out_zip

    def test_round_trip_registers_library(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        out_zip = self._make_bundle(src_config, src_sample_folder, tmp_path)

        # Switch to destination DB
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))

        dest_dir = tmp_path / "imported"
        results = bundle_mod.import_bundle(out_zip, dest_dir)

        assert results["samples"]["added"] == 1
        assert results["samples"]["skipped"] == 0

        libs = library_mod.list_entries()["libraries"]
        assert len(libs) == 1
        assert libs[0]["name"] == "my-samples"

    def test_round_trip_file_rows_recreated(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        out_zip = self._make_bundle(src_config, src_sample_folder, tmp_path)
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))

        dest_dir = tmp_path / "imported"
        bundle_mod.import_bundle(out_zip, dest_dir)

        result = library_mod.show("my-samples")
        assert len(result["files"]) == 3
        refs = {f["ref"] for f in result["files"]}
        assert any("kick_808.wav" in r for r in refs)
        assert any("snare_tight.wav" in r for r in refs)

    def test_round_trip_sample_files_extracted(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        out_zip = self._make_bundle(src_config, src_sample_folder, tmp_path)
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))

        dest_dir = tmp_path / "imported"
        bundle_mod.import_bundle(out_zip, dest_dir)

        # Physical files should exist under dest_dir/samples/my-samples/
        samples_root = dest_dir / "samples" / "my-samples"
        all_wavs = list(samples_root.rglob("*.wav"))
        assert len(all_wavs) == 3

    def test_import_is_idempotent(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        out_zip = self._make_bundle(src_config, src_sample_folder, tmp_path)
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        dest_dir = tmp_path / "imported"

        r1 = bundle_mod.import_bundle(out_zip, dest_dir)
        r2 = bundle_mod.import_bundle(out_zip, dest_dir)

        assert r1["samples"]["added"] == 1
        assert r2["samples"]["updated"] == 1
        assert r2["samples"]["added"] == 0
        # still only one library in the DB
        assert len(library_mod.list_entries()["libraries"]) == 1

    def test_library_path_points_to_dest_dir(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        """After import, library.path should point to dest_dir, not the source machine's path."""
        out_zip = self._make_bundle(src_config, src_sample_folder, tmp_path)
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))

        dest_dir = tmp_path / "imported"
        bundle_mod.import_bundle(out_zip, dest_dir)

        libs = library_mod.list_entries()["libraries"]
        lib_path = Path(libs[0]["path"])
        assert lib_path.is_relative_to(dest_dir)

    def test_tags_preserved_on_import(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        library_mod.add("my-samples", str(src_sample_folder), tags=["drums", "lofi"])
        out_zip = tmp_path / "bundle.zip"
        bundle_mod.export_bundle(out_zip, include=["samples"])

        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        dest_dir = tmp_path / "imported"
        bundle_mod.import_bundle(out_zip, dest_dir)

        libs = library_mod.list_entries()["libraries"]
        assert set(libs[0]["tags"]) == {"drums", "lofi"}

    def test_missing_zip_raises(self, src_config, tmp_path, monkeypatch, dst_config):
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        with pytest.raises(FileNotFoundError):
            bundle_mod.import_bundle(tmp_path / "nonexistent.zip", tmp_path / "out")

    def test_not_a_zip_raises(self, src_config, tmp_path, monkeypatch, dst_config):
        bad_file = tmp_path / "not_a_zip.zip"
        bad_file.write_bytes(b"this is not a zip")
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        with pytest.raises((ValueError, zipfile.BadZipFile)):
            bundle_mod.import_bundle(bad_file, tmp_path / "out")

    def test_multiple_libraries_bundled_and_imported(self, src_config, tmp_path, monkeypatch, dst_config):
        folder_a = tmp_path / "lib-a"
        folder_b = tmp_path / "lib-b"
        _write_wav(folder_a / "kick.wav")
        _write_wav(folder_b / "pad.wav")

        library_mod.add("lib-a", str(folder_a), tags=["drums"])
        library_mod.add("lib-b", str(folder_b), tags=["melodic"])
        out_zip = tmp_path / "multi.zip"
        bundle_mod.export_bundle(out_zip, include=["samples"])

        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        dest_dir = tmp_path / "imported"
        results = bundle_mod.import_bundle(out_zip, dest_dir)

        assert results["samples"]["added"] == 2
        libs = library_mod.list_entries()["libraries"]
        assert {l["name"] for l in libs} == {"lib-a", "lib-b"}


class TestDbBackupRestore:
    """Full-DB snapshot backup/restore (catalog only, no audio copied)."""

    def test_backup_creates_db_file(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("snap", str(src_sample_folder), tags=["drums"])
        out = tmp_path / "backup.db"
        res = bundle_mod.export_db(out)
        assert res["format"] == "db"
        assert Path(res["path"]).is_file()
        assert res["bytes"] > 0

    def test_backup_appends_db_suffix(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("snap", str(src_sample_folder))
        res = bundle_mod.export_db(tmp_path / "nosuffix")
        assert res["path"].endswith(".db")

    def test_backup_requires_existing_db(self, tmp_path, monkeypatch):
        empty = tmp_path / "void" / "library.db"
        empty.parent.mkdir(parents=True)
        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(empty))
        with pytest.raises(FileNotFoundError):
            bundle_mod.export_db(tmp_path / "x.db")

    def test_restore_onto_fresh_machine(self, src_config, src_sample_folder, tmp_path, monkeypatch, dst_config):
        library_mod.add("snap", str(src_sample_folder), tags=["drums"])
        out = tmp_path / "backup.db"
        bundle_mod.export_db(out)

        monkeypatch.setenv(library_mod.CONFIG_ENV_VAR, str(dst_config))
        res = bundle_mod.import_db(out)
        assert res["libraries"] == 1
        libs = library_mod.list_entries()["libraries"]
        assert {l["name"] for l in libs} == {"snap"}

    def test_restore_refuses_to_clobber_without_overwrite(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("snap", str(src_sample_folder))
        out = tmp_path / "backup.db"
        bundle_mod.export_db(out)
        # src_config DB is non-empty; restoring over it must refuse.
        with pytest.raises(ValueError):
            bundle_mod.import_db(out)

    def test_restore_overwrite_replaces_catalog(self, src_config, src_sample_folder, tmp_path):
        library_mod.add("snap", str(src_sample_folder))
        out = tmp_path / "backup.db"
        bundle_mod.export_db(out)
        # Add another library, then restore the older snapshot over it.
        other = tmp_path / "other"
        _write_wav(other / "clap.wav")
        library_mod.add("extra", str(other))
        bundle_mod.import_db(out, overwrite=True)
        libs = library_mod.list_entries()["libraries"]
        assert {l["name"] for l in libs} == {"snap"}

    def test_restore_rejects_non_mendell_db(self, src_config, tmp_path):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not a sqlite db at all")
        with pytest.raises(Exception):
            bundle_mod.import_db(junk, overwrite=True)
