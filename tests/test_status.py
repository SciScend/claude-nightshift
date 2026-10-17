from claude_nightshift.status import (
    STATUS_FILE,
    STATUS_INSTRUCTION,
    extract_task_from_command,
    inject_prompt,
    read_status_file,
    set_status_file,
    status_file,
    status_instruction,
)


def test_extract_task_from_command():
    assert extract_task_from_command(["claude", "-p", "do it"]) == "do it"
    assert extract_task_from_command(["claude", "--print", "do it"]) == "do it"
    assert extract_task_from_command(["claude", "-p"]) is None
    assert extract_task_from_command(["claude", "-p", "--verbose"]) is None
    assert extract_task_from_command(["claude", "-c"]) is None


def test_inject_prompt_returns_a_new_list():
    cmd = ["claude", "-p", "task", "--verbose"]
    new = inject_prompt(cmd, " more")
    assert new == ["claude", "-p", "task more", "--verbose"]
    assert cmd == ["claude", "-p", "task", "--verbose"]


def test_inject_prompt_without_a_prompt_is_a_no_op():
    assert inject_prompt(["claude", "-c"], " more") == ["claude", "-c"]


def test_read_status_file(tmp_path):
    path = tmp_path / "status.md"
    assert read_status_file(str(path)) is None
    path.write_text("  done: A\n")
    assert read_status_file(str(path)) == "done: A"


def test_status_file_defaults_to_the_working_directory():
    assert status_file() == STATUS_FILE
    assert status_instruction() == STATUS_INSTRUCTION


def test_set_status_file_changes_the_instruction_and_the_reader(tmp_path):
    path = tmp_path / "state" / "status.md"
    path.parent.mkdir()
    path.write_text("done: B\n")
    set_status_file(str(path))
    assert status_file() == str(path)
    assert str(path) in status_instruction()
    assert STATUS_FILE not in status_instruction()
    assert read_status_file() == "done: B"
    set_status_file(None)
    assert status_file() == STATUS_FILE
