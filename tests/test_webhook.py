

def test_classify_command():
    from ferry.webhook import classify_command
    assert classify_command("/ferry plan homelab")[0] == "run"
    kind, m = classify_command("/ferry unlock --inventory=homelab\nbecause it's stuck")
    assert kind == "unlock" and m.group(1) == "homelab"
    assert classify_command("/ferry unlock")[1].group(1) is None
    assert classify_command("  /ferry help ") == ("help", None)
    for bad in ("/ferry", "/ferry aply", "/ferry plan prod extra", "/ferry unlock a b"):
        assert classify_command(bad) == ("bad", None), bad
    for other in ("", "lgtm", "/ferryman", "/plan", "see /ferry help", "> /ferry plan"):
        assert classify_command(other) is None, other
