import json
import shutil
import textwrap

import pytest

from ferry import config
from ferry.policy import check
from ferry.runner import AnsibleRunner, RunRequest
from ferry.sanitize import MASK, Redactor


def test_redactor_env_values_and_patterns():
    red = Redactor(env_vars=["SITE_PW"], environ={"SITE_PW": "correct-horse", "FERRY_GITLAB_TOKEN": "glpat-abcdefghijklmnopqrst",
                                                 "HOME": "/home/pip", "SHORT_TOKEN": "abc"})
    out = red.text("pw correct-horse tok glpat-abcdefghijklmnopqrst home /home/pip abc password=swordfish $6$saltsalt$hashhashhash")
    assert "correct-horse" not in out and "glpat-" not in out and "swordfish" not in out and "$6$salt" not in out
    assert "/home/pip" in out and "abc" in out
    assert out.count(MASK) >= 4


def test_redactor_sensitive_paths():
    red = Redactor(sensitive_paths=["/etc/shadow*", "*.key"], environ={})
    assert red.sensitive_path("/etc/shadow") and red.sensitive_path("/etc/ssl/a.key")
    assert not red.sensitive_path("/etc/chrony/chrony.conf")


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "ferry.yaml").write_text("apply: {min_aprovals: 2}\n")
    with pytest.raises(ValueError, match="min_aprovals"):
        config.load(tmp_path / "ferry.yaml")


def test_config_digest_changes_with_content(tmp_path):
    (tmp_path / "a.yaml").write_text("apply: {min_approvals: 1}\n")
    (tmp_path / "b.yaml").write_text("apply: {min_approvals: 2}\n")
    assert config.load(tmp_path / "a.yaml").digest() != config.load(tmp_path / "b.yaml").digest()


def test_policy_allows_guard_and_denies_others(repo):
    cfg = config.load(repo / "ferry.yaml")
    assert check(repo, cfg.policy) == []  # printer_guard's check_mode: false is allow-listed
    (repo / "roles/base/tasks/main.yml").write_text(textwrap.dedent("""
        - name: sneaky
          ansible.builtin.debug: {msg: "{{ lookup('pipe', 'cat ~/.ssh/id_ed25519') }}"}
        # check_mode: false   (comments are fine)
        - name: reboot
          ansible.builtin.reboot:
    """))
    problems = check(repo, cfg.policy)
    assert any("pipe" in p for p in problems) and any("reboot" in p for p in problems)
    assert not any(":4:" in p for p in problems)


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
def test_callback_plugin_real_ansible_run_respects_no_log(tmp_path):
    """Integration: real ansible-playbook, local connection, check+diff, through ferry_events."""
    (tmp_path / "inv.yml").write_text("all: {hosts: {testhost: {ansible_connection: local, ansible_python_interpreter: auto_silent}}}\n")
    target = tmp_path / "managed.txt"
    target.write_text("old\n")
    (tmp_path / "pb.yml").write_text(textwrap.dedent(f"""
        - hosts: all
          gather_facts: false
          tasks:
            - name: Change a file
              ansible.builtin.copy: {{content: "new\\n", dest: "{target}"}}
            - name: Secret file
              ansible.builtin.copy: {{content: "TOPSECRET-VALUE\\n", dest: "{tmp_path}/secret.txt"}}
              no_log: true
            - name: Busy guard
              ansible.builtin.fail: {{msg: "FERRY_HOST_BUSY: printer is printing"}}
    """))
    res = AnsibleRunner(tmp_path, "FERRY_HOST_BUSY").run(RunRequest("pb.yml", "inv.yml", None, check=True, diff=True))
    assert target.read_text() == "old\n"  # check mode
    h = res.hosts["testhost"]
    assert h.blocked == "printer is printing" and h.failed == 0
    changes = {c.task: c for c in res.changes}
    assert any("+new" in json.dumps(d) or d.get("after") == "new\n" for d in changes["Change a file"].diffs)
    assert changes["Secret file"].no_log and changes["Secret file"].diffs == []
    assert "TOPSECRET" not in json.dumps([c.__dict__ for c in res.changes])
