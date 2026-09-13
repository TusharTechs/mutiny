

def test_tarball_keeps_what_an_install_needs(tmp_path):
    """Trimming the archive must not drop the files packaging metadata points at.

    Excluding README.md once made flask's editable install fail, which turned
    into an import error three stages later — so assert on the contents, not
    just the size.
    """
    import io
    import tarfile

    from mutiny.sandbox import tarball

    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nreadme = "README.md"\n')
    (tmp_path / "README.md").write_text("# pkg\n")
    (tmp_path / "LICENSE").write_text("Apache\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("z" * 5000)
    (tmp_path / "src" / "pkg" / "logo.png").write_bytes(b"\x89PNG" + b"\0" * 5000)

    names = set(tarfile.open(fileobj=io.BytesIO(tarball(tmp_path))).getnames())
    assert {"src/pkg/__init__.py", "pyproject.toml", "README.md", "LICENSE"} <= names
    assert "docs/guide.md" not in names
    assert "src/pkg/logo.png" not in names
