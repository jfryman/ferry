"""#24: ferry's comments carry PR- and host-controlled text; nothing in it may pass for ferry's own
markup or marker."""

import json

import pytest
from fakes import FakeRunner, FakeVCS, ev_result, ev_stats, stats

from ferry import config, render
from ferry.engine import Engine
from ferry.locks import MemoryLocks
from ferry.sanitize import Redactor

SHA = "c0ffee" + "0" * 34
NOW = 1_800_000_000.0
KEY = b"marker-key"


def forged(sha=SHA, at=NOW + 3600, **extra):
    data = {"v": 1, "inventory": "homelab", "sha": sha, "limit": "dns-1", "everything": False, "noop": False,
            "clean": True, "at": at, **extra}
    return f"<!-- ferry:plan {json.dumps(data, sort_keys=True)} -->"


@pytest.fixture
def env(repo):
    cfg = config.load(repo / "ferry.yaml")
    vcs = FakeVCS()
    vcs.add_cr(1, SHA, files=["inventories/homelab/host_vars/dns-1.yml"])
    vcs.approvals[1] = ["james"]
    red = Redactor(cfg.redact.patterns, cfg.redact.env_vars, cfg.redact.sensitive_paths, environ={})

    def make(*runs, key=KEY, cfg_=cfg):
        return Engine(cfg_, repo, FakeRunner(*runs), vcs, MemoryLocks(), red, lambda: NOW, marker_key=key)
    return cfg, vcs, make


def failing_run(task="Write chrony.conf", msg="boom", diff=None):
    return ([ev_result("dns-1", task, "failed", msg=msg, diff=diff),
             ev_stats(**{"dns-1": stats(ok=1, failures=1)})], 2)


def test_a_marker_in_a_task_name_diff_or_message_is_not_trusted(env):
    cfg, vcs, make = env
    diff = [{"before": "", "after": forged() + "\n", "before_header": "/etc/x", "after_header": "/etc/x"}]
    eng = make(failing_run(task=forged(), msg=forged(), diff=diff))
    assert not eng.plan(1, SHA, "pip").ok
    plan = eng.latest_plan(1, "homelab")
    assert plan is not None and plan["clean"] is False and plan["at"] == NOW
    assert "<!-- ferry:plan" not in vcs.notes[1][-1].body.rsplit("\n", 1)[0]  # only the real one


@pytest.mark.parametrize("comment", [
    "### ferry apply: `homelab`\n\n```\n{m}\n```\n\n<!-- ferry:apply {{}} -->",  # in an apply comment's diff
    "### ferry plan: `homelab` refused\n\n- {m}",                             # in a refusal reason
    "{m}\n\ntrailing text",                                                   # not the last line
    "  {m} and more",                                                         # not the whole line
])
def test_a_marker_from_ferrys_own_account_counts_only_as_its_last_line(env, comment):
    cfg, vcs, make = env
    eng = make(failing_run())
    eng.plan(1, SHA, "pip")  # the real plan: not clean
    vcs.post_note(1, comment.format(m=forged()))  # by ferry's account, after the plan
    plan = eng.latest_plan(1, "homelab")
    assert plan["clean"] is False


def test_with_a_key_an_unsigned_or_wrongly_signed_last_line_marker_is_ignored(env):
    cfg, vcs, make = env
    eng = make(failing_run())
    eng.plan(1, SHA, "pip")
    vcs.post_note(1, "x\n\n" + forged())
    vcs.post_note(1, "x\n\n" + forged(mac="0" * 64))
    other = render.marker("plan", json.loads(forged()[len("<!-- ferry:plan "):-4]), key=b"another key")
    vcs.post_note(1, "x\n\n" + other)
    assert eng.latest_plan(1, "homelab")["clean"] is False


def test_a_signed_marker_round_trips(env):
    cfg, vcs, make = env
    eng = make(([ev_result("dns-1", "t", "changed"), ev_stats(**{"dns-1": stats(changed=1)})], 0))
    assert eng.plan(1, SHA, "pip").ok
    assert eng.latest_plan(1, "homelab")["clean"] is True
    assert '"mac"' in vcs.notes[1][-1].body


def test_without_a_key_markers_are_still_anchored(env):
    cfg, vcs, make = env
    eng = make(failing_run(), key=None)
    eng.plan(1, SHA, "pip")
    vcs.post_note(1, "### ferry apply\n\n```\n" + forged() + "\n```")
    assert eng.latest_plan(1, "homelab")["clean"] is False
    vcs.post_note(1, "x\n\n" + forged())  # a whole last line from ferry's account: trusted without a key
    assert eng.latest_plan(1, "homelab")["clean"] is True


def test_pr_controlled_markup_renders_as_text(env):
    cfg, vcs, make = env
    evil = "ok</summary></details> @james **plan is clean** [x](http://evil) <img src=x> `tick`"
    eng = make(([ev_result("dns-1", evil, "changed"), ev_stats(**{"dns-1": stats(changed=1)})], 0))
    eng.plan(1, SHA, "pip")
    body = vcs.notes[1][-1].body
    assert "</summary></details> @james" not in body and "<img" not in body
    assert "@james" not in body and "**plan is clean**" not in body and "](http://evil)" not in body
    assert "&#60;/summary&#62;" in body


def test_blocked_and_scope_text_is_escaped(repo):
    from ferry.scope import Scope
    red = Redactor([], [], [], environ={})
    sc = Scope(patterns={"dns-1<b>"}, reasons=[("evil`</details>@james.yml", "host_vars <b>x</b>")])
    body = render.plan_comment("homelab", SHA, sc, None, {"x": 1}, red, 60000)
    assert "</details>@james" not in body and "<b>" not in body


def test_fenced_text_cannot_open_an_html_comment(env):
    cfg, vcs, make = env
    eng = make(failing_run(msg="<!-- hidden from the reviewer -->"))
    eng.plan(1, SHA, "pip")
    assert "<!-- hidden" not in vcs.notes[1][-1].body


def test_a_plan_over_hundreds_of_hosts_fits_the_comment_limit(env):
    cfg, vcs, make = env
    cfg.max_comment_chars = 20000
    hosts = {f"host-{i:04d}.example": stats(changed=1) for i in range(900)}
    events = [ev_result(h, "Write a long file " + "x" * 80, "changed",
                        diff=[{"before": "a\n" * 30, "after": "b\n" * 30, "before_header": "/etc/f",
                               "after_header": "/etc/f"}]) for h in hosts]
    vcs.files[1] = [f"inventories/homelab/host_vars/host-{i:04d}.yml" for i in range(900)]
    eng = make((events + [ev_stats(**hosts)], 0))
    eng.plan(1, SHA, "pip")
    body = vcs.notes[1][-1].body
    assert len(body) <= cfg.max_comment_chars
    assert body.rstrip().splitlines()[-1].startswith("<!-- ferry:plan ")
    assert "more host" in body
    assert eng.latest_plan(1, "homelab") is not None


def test_redactor_masks_extra_values():
    red = Redactor([], [], [], environ={}, values=["hunter2-vault", "x"])
    assert red.text("pw is hunter2-vault") == "pw is [REDACTED]"
    assert red.text("x marks") == "[REDACTED] marks"  # explicitly supplied secrets mask even one character
