import pytest

from council.tools import shell
from council.tools.workspace import PathEscape, Workspace


def test_workspace_write_read_list(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("pkg/mod.py", "x = 1\n")
    assert ws.read_file("pkg/mod.py") == "x = 1\n"
    assert "pkg/mod.py" in ws.list_files()


def test_workspace_blocks_path_escape(tmp_path):
    ws = Workspace(tmp_path / "ws")
    with pytest.raises(PathEscape):
        ws.write_file("../escape.py", "nope")
    with pytest.raises(PathEscape):
        ws.read_file("/etc/passwd")


def test_shell_runs_in_workspace(tmp_path):
    ws = Workspace(tmp_path / "ws")
    ws.write_file("hello.py", "print('hi')\n")
    res = shell.run_command("python3 hello.py", ws.root, timeout=20)
    assert res.ok
    assert "hi" in res.stdout


def test_shell_denylist(tmp_path):
    with pytest.raises(shell.DeniedCommand):
        shell.run_command("rm -rf /", tmp_path, timeout=5)


def test_shell_timeout(tmp_path):
    res = shell.run_command("python3 -c 'import time; time.sleep(5)'", tmp_path, timeout=1)
    assert res.timed_out and not res.ok
