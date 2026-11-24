import pytest

from fakes import FakeRunner, FakeVCS, ev_result, ev_stats, stats
from ferry import config, render
from ferry.engine import Engine
from ferry.locks import MemoryLocks, new_lock
from ferry.sanitize import Redactor

SHA = "c0ffee" + "0" * 34
SHA2 = "beef00" + "0" * 34
NOW = 1_800_000_000.0


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def clean_plan_run():
    diff = [{"before": "server old\n", "after": "server 10.0.0.4 iburst prefer trust\n",
             "before_header": "/etc/chrony/chrony.conf", "after_header": "/etc/chrony/chrony.conf"}]
    return ([ev_result("dns-1", "Write chrony.conf", "changed", role="chrony", diff=diff),
             ev_stats(**{"dns-1": stats(ok=5, changed=1)})], 0)


@pytest.fixture
def env(repo):
    cfg = config.load(repo / "ferry.yaml")
    vcs = FakeVCS()
    locks = MemoryLocks()
    clock = Clock()
    red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths, environ={})

    def make(*runs):
        runner = FakeRunner(*runs)
        return Engine(cfg, repo, runner, vcs, locks, red, clock), runner

    return cfg, vcs, locks, clock, make


def test_plan_noop_for_docs_only(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["README.md"])
    eng, runner = make()
    out = eng.plan(1, SHA, "pip")
    assert out.ok and runner.requests == []
    assert "Nothing to plan" in vcs.notes[1][-1].body
    assert locks.get("homelab") is None  # noop plans don't lock


def test_plan_runs_check_diff_with_limit_and_comments(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/dns-1.yml"])
    eng, runner = make(clean_plan_run())
    out = eng.plan(1, SHA, "pip")
    assert out.ok
    req = runner.requests[0]
    assert (req.check, req.diff, req.limit, req.inventory) == (True, True, "dns-1", "inventories/homelab")
    body = vcs.notes[1][-1].body
    assert "+server 10.0.0.4 iburst prefer trust" in body
    kind, meta = render.parse_markers(body)[0]
    assert kind == "plan" and meta["sha"] == SHA and meta["clean"] and meta["limit"] == "dns-1"
    assert locks.get("homelab").holder == 1


def test_plan_refused_while_other_open_mr_holds_lock(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/dns-1.yml"])
    vcs.add_cr(2, SHA2, files=["inventories/homelab/host_vars/dns-1.yml"])
    locks.try_acquire(new_lock("homelab", 1, SHA, "pip"))
    eng, runner = make()
    out = eng.plan(2, SHA2, "pip")
    assert not out.ok and runner.requests == []
    assert "locked by !1" in vcs.notes[2][-1].body


def test_stale_lock_from_merged_mr_is_taken_over(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=[], state="merged")
    vcs.add_cr(2, SHA2, files=["inventories/homelab/host_vars/dns-1.yml"])
    locks.try_acquire(new_lock("homelab", 1, SHA, "pip"))
    eng, _ = make(clean_plan_run())
    assert eng.plan(2, SHA2, "pip").ok
    assert locks.get("homelab").holder == 2


def planned(env, approvals=("james",), author="pip"):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, author=author, files=["inventories/homelab/host_vars/dns-1.yml"])
    vcs.approvals[1] = list(approvals)
    eng, _ = make(clean_plan_run())
    assert eng.plan(1, SHA, "pip").ok


def test_apply_happy_path(env):
    cfg, vcs, locks, clock, make = env
    planned(env)
    eng, runner = make(clean_plan_run())
    out = eng.apply(1, SHA, "james")
    assert out.ok, out.reasons
    req = runner.requests[0]
    assert (req.check, req.limit) == (False, "dns-1")
    assert "ferry apply" in vcs.notes[1][-1].body and "applied" in vcs.notes[1][-1].body
    assert locks.get("homelab").holder == 1  # held until merge


@pytest.mark.parametrize("mutate, expect", [
    (lambda vcs, clock: setattr(vcs.crs[1], "head_sha", SHA2), "MR head"),
    (lambda vcs, clock: vcs.approvals.__setitem__(1, []), "needs 1 approval"),
    (lambda vcs, clock: vcs.approvals.__setitem__(1, ["pip"]), "other than the author"),
    (lambda vcs, clock: setattr(vcs.crs[1], "draft", True), "draft"),
    (lambda vcs, clock: setattr(vcs.crs[1], "has_conflicts", True), "conflicts"),
    (lambda vcs, clock: setattr(vcs.crs[1], "discussions_resolved", False), "discussions"),
    (lambda vcs, clock: setattr(vcs.crs[1], "blocked_reasons", ["need_rebase"]), "need_rebase"),
    (lambda vcs, clock: setattr(vcs.crs[1], "state", "closed"), "MR is closed"),
    (lambda vcs, clock: vcs.rule_blockers.__setitem__(1, ["GitHub requires check `lint`"]), "`lint`"),
    (lambda vcs, clock: setattr(clock, "t", NOW + 25 * 3600), "old"),
    (lambda vcs, clock: vcs.files.__setitem__(1, ["inventories/homelab/host_vars/ntp-1.yml"]), "scope differs"),
])
def test_apply_refusals(env, mutate, expect):
    cfg, vcs, locks, clock, make = env
    planned(env)
    mutate(vcs, clock)
    eng, runner = make()
    sha = SHA
    out = eng.apply(1, sha, "james")
    assert not out.ok
    assert runner.requests == []  # nothing ran
    assert any(expect in r for r in out.reasons), out.reasons
    assert "refused" in vcs.notes[1][-1].body


def test_apply_refused_for_non_developer(env):
    cfg, vcs, locks, clock, make = env
    planned(env)
    eng, runner = make()
    out = eng.apply(1, SHA, "mallory")
    assert not out.ok and runner.requests == []
    assert any("Developer" in r for r in out.reasons)


@pytest.mark.parametrize("approvals", [["mallory"], ["ferry-bot"], ["mallory", "ferry-bot"]])
def test_apply_ignores_approvals_without_write_access(env, approvals):
    # On a public GitHub repo anyone can leave an approving review; ferry's own account never counts.
    cfg, vcs, locks, clock, make = env
    planned(env, approvals=approvals)
    eng, runner = make()
    out = eng.apply(1, SHA, "james")
    assert not out.ok and runner.requests == []
    assert any("needs 1 approval" in r and "has 0" in r for r in out.reasons), out.reasons


def test_apply_counts_approvals_with_write_access(env):
    cfg, vcs, locks, clock, make = env
    planned(env, approvals=["mallory", "james"])
    eng, runner = make(clean_plan_run())
    assert eng.apply(1, SHA, "james").ok


def test_allowed_users_still_needs_write_access(env):
    cfg, vcs, locks, clock, make = env
    cfg.apply.allowed_users = ["james", "ex-member"]
    planned(env)
    eng, runner = make()
    out = eng.apply(1, SHA, "ex-member")
    assert not out.ok and runner.requests == []
    assert any("may not apply" in r for r in out.reasons), out.reasons


def test_apply_ignores_forged_plan_markers(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/dns-1.yml"])
    vcs.approvals[1] = ["james"]
    forged = render.marker("plan", {"inventory": "homelab", "sha": SHA, "clean": True, "limit": "dns-1",
                                    "config": cfg.digest(), "at": NOW, "noop": False})
    vcs.post_note(1, "looks legit " + forged, author="mallory")
    eng, runner = make()
    out = eng.apply(1, SHA, "james")
    assert not out.ok and "no ferry plan on this MR yet" in out.reasons


def test_apply_refused_when_plan_not_clean(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/voron.yml"])
    vcs.approvals[1] = ["james"]
    blocked = ([ev_result("voron", "Refuse while printing", "failed", role="printer_guard",
                          msg="FERRY_HOST_BUSY: printer is printing (benchy.gcode)"),
                ev_stats(voron=stats(ok=1, failures=1))], 2)
    eng, _ = make(blocked)
    plan = eng.plan(1, SHA, "pip")
    assert not plan.ok
    assert plan.run.hosts["voron"].state == "blocked"
    assert "printer is printing" in vcs.notes[1][-1].body
    out = eng.apply(1, SHA, "james")
    assert not out.ok and any("not clean" in r for r in out.reasons)


def test_unlock_after_merge(env):
    cfg, vcs, locks, clock, make = env
    planned(env)
    vcs.commit_mrs["m3rge"] = [1]
    eng, _ = make()
    assert eng.unlock_for_commit("m3rge") == [1]
    assert locks.get("homelab") is None


def test_policy_violation_blocks_plan(env, repo):
    cfg, vcs, locks, clock, make = env
    (repo / "roles/base/tasks/main.yml").write_text("- ansible.builtin.command: id\n  check_mode: false\n")
    vcs.add_cr(1, SHA, files=["roles/base/tasks/main.yml"])
    eng, runner = make()
    out = eng.plan(1, SHA, "pip")
    assert not out.ok and runner.requests == []
    assert any("roles/base/tasks/main.yml:2" in r for r in out.reasons)


def test_plan_comment_redacts_and_hides(env):
    cfg, vcs, locks, clock, make = env
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/dns-1.yml"])
    run = ([
        ev_result("dns-1", "Template app config", "changed", diff=[{
            "before": "", "after": "password: hunter2hunter2\napi_token=hvs.CAESIabcdefghijklmnopqrstuvwxyz\n",
            "before_header": "/etc/app.conf", "after_header": "/etc/app.conf"}]),
        ev_result("dns-1", "Install key", "changed", diff=[{
            "before": "", "after": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----\n",
            "before_header": "/etc/ssl/private/x.key", "after_header": "/etc/ssl/private/x.key"}]),
        ev_result("dns-1", "Secret thing", "changed", no_log=True),
        ev_stats(**{"dns-1": stats(changed=3)}),
    ], 0)
    eng, _ = make(run)
    eng.plan(1, SHA, "pip")
    body = vcs.notes[1][-1].body
    assert "hunter2" not in body and "hvs.CAES" not in body and "BEGIN OPENSSH" not in body
    assert "sensitive path, diff hidden" in body
    assert "no_log task: changed, details hidden" in body


def test_drift_writes_metrics_for_reachable_hosts_only(env, tmp_path):
    cfg, vcs, locks, clock, make = env
    check = ([
        ev_result("dns-1", "chrony.conf", "changed"),
        ev_result("voron", "guard", "failed", msg="FERRY_HOST_BUSY: printing"),
        ev_result("adsb-1", "Gathering Facts", "unreachable", msg="ssh: timeout"),
        ev_stats(**{"dns-1": stats(changed=1), "voron": stats(failures=1), "adsb-1": stats(unreachable=1),
                    "ntp-1": stats(ok=9)}),
    ], 2)
    eng, runner = make(check, ([ev_stats()], 0))
    out = eng.drift("abc123def456", out_dir=tmp_path / "out")
    assert out.extra["drifted"] == ["dns-1"]
    assert not out.ok  # adsb-1 unreachable
    report = runner.requests[1]
    assert report.check is False and report.limit == "dns-1,ntp-1"
    assert report.playbook.endswith("drift_report.yml")
    prom = (tmp_path / "out/drift.prom").read_text()
    assert 'ansible_drift_host_state{inventory="homelab",host="voron",state="blocked"} 1' in prom
