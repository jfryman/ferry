import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip())


@pytest.fixture
def repo(tmp_path):
    """A small config repo shaped like infra/ansible."""
    write(tmp_path, "ferry.yaml", """
        inventories:
          - {name: homelab, path: inventories/homelab}
        policy:
          check_mode_false_allow: ["roles/printer_guard/**"]
        apply:
          max_plan_age_hours: 24
    """)
    write(tmp_path, "site.yml", """
        - name: Guard
          hosts: printers
          gather_facts: false
          roles: [printer_guard]
        - ansible.builtin.import_playbook: playbooks/base.yml
        - ansible.builtin.import_playbook: playbooks/printers.yml
    """)
    write(tmp_path, "playbooks/base.yml", """
        - name: Base
          hosts: all
          roles: [base, chrony]
    """)
    write(tmp_path, "playbooks/printers.yml", """
        - name: Printers
          hosts: printers
          tasks:
            - ansible.builtin.include_role:
                name: klipper_extras
    """)
    write(tmp_path, "roles/base/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
    write(tmp_path, "roles/chrony/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
    write(tmp_path, "roles/printer_guard/tasks/main.yml", """
        - name: Query
          ansible.builtin.uri: {url: "http://127.0.0.1:7125"}
          check_mode: false
    """)
    write(tmp_path, "roles/klipper_extras/meta/main.yml", "dependencies: [moonraker_common]\n")
    write(tmp_path, "roles/klipper_extras/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
    write(tmp_path, "roles/moonraker_common/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
    write(tmp_path, "roles/unused/tasks/main.yml", "- ansible.builtin.debug: {msg: hi}\n")
    write(tmp_path, "inventories/homelab/hosts.yml", "all: {children: {printers: {hosts: {voron: {}}}}}\n")
    return tmp_path
