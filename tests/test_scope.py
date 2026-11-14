from ferry import config
from ferry.scope import compute


def scope(repo, *files):
    cfg = config.load(repo / "ferry.yaml")
    return compute(repo, list(files), "inventories/homelab", "site.yml", cfg.scope.ignore)


def test_docs_only_is_noop(repo):
    s = scope(repo, "README.md", "lode/summary.md", ".gitlab-ci.yml", "tests/x.py")
    assert s.empty and s.limit == ""


def test_host_and_group_vars(repo):
    s = scope(repo, "inventories/homelab/host_vars/voron.yml", "inventories/homelab/group_vars/dns/main.yml")
    assert s.patterns == {"voron", "dns"} and s.limit == "dns:voron"


def test_group_all_is_everything(repo):
    s = scope(repo, "inventories/homelab/group_vars/all.yml")
    assert s.everything and s.limit is None


def test_role_used_by_all_play(repo):
    assert scope(repo, "roles/chrony/templates/chrony.conf.j2").everything


def test_role_used_by_group_play_only(repo):
    s = scope(repo, "roles/printer_guard/tasks/main.yml")
    assert not s.everything and s.patterns == {"printers"}


def test_include_role_and_transitive_dependency(repo):
    assert scope(repo, "roles/klipper_extras/tasks/main.yml").patterns == {"printers"}
    # moonraker_common is only reached through klipper_extras' meta dependency
    assert scope(repo, "roles/moonraker_common/defaults/main.yml").patterns == {"printers"}


def test_unused_role_affects_nothing(repo):
    s = scope(repo, "roles/unused/tasks/main.yml")
    assert s.empty
    assert "not used" in s.reasons[0][1]


def test_imported_playbook(repo):
    assert scope(repo, "playbooks/printers.yml").patterns == {"printers"}
    assert scope(repo, "playbooks/base.yml").everything
    assert scope(repo, "site.yml").everything


def test_unknown_files_are_everything(repo):
    for f in ("ansible.cfg", "inventories/homelab/hosts.yml", "requirements.yml", "ferry.yaml", "filter_plugins/x.py"):
        assert scope(repo, f).everything, f


def test_complex_host_pattern_widens(repo):
    (repo / "playbooks/printers.yml").write_text("- hosts: 'printers:&canary'\n  roles: [klipper_extras]\n")
    assert scope(repo, "roles/klipper_extras/tasks/main.yml").everything
