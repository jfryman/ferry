import os
import sys
import textwrap
import time

from ferry.runner import AnsibleRunner, RunRequest


def fake_playbook(tmp_path, body):
    script = tmp_path / "fake-ansible-playbook"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    return str(script)


def test_run_is_killed_after_the_timeout(tmp_path):
    # A child that would outlive its parent (like an ssh connection) is killed with the process group.
    pb = fake_playbook(tmp_path, """
        import subprocess, time
        subprocess.Popen(["sleep", "60"])
        print("started", flush=True)
        time.sleep(60)
    """)
    runner = AnsibleRunner(tmp_path, "BUSY", ansible_playbook=pb, timeout=1)
    t = time.monotonic()
    result = runner.run(RunRequest("site.yml", "inv"))
    assert time.monotonic() - t < 20
    assert not result.clean and "job timeout" in result.log_tail


def test_echo_is_redacted(tmp_path, capsys):
    pb = fake_playbook(tmp_path, """
        import os
        print("token is " + os.environ["VAULT_TOKEN"], flush=True)
    """)
    runner = AnsibleRunner(tmp_path, "BUSY", ansible_playbook=pb, env={"VAULT_TOKEN": "s.sekrit"},
                           echo=lambda line: line.replace("s.sekrit", "***"))
    runner.run(RunRequest("site.yml", "inv"))
    out = capsys.readouterr().out
    assert "token is ***" in out and "s.sekrit" not in out
