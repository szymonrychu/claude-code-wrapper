from claude_code_wrapper.redact import Redactor


def test_redactor_longest_first():
    r = Redactor(["abcdefgh", "abcdefghijkl"])
    assert r.redact("x abcdefghijkl y abcdefgh") == "x *** y ***"


def test_short_values_ignored():
    assert Redactor(["short"]).redact("a short b") == "a short b"


def test_redactor_patterns():
    r = Redactor()
    assert "sk-ant-api03-AbC_d-ef" not in r.redact("key sk-ant-api03-AbC_d-ef end")
    assert r.redact("Authorization: Bearer abc.def~123+/=") == "Authorization: Bearer ***"
    assert r.redact("t ghp_" + "a" * 20) == "t ***"
    assert r.redact("t glpat-" + "b" * 20) == "t ***"
    assert r.redact("git https://user:s3cret@host/x") == "git https://user:***@host/x"


def test_redact_obj_recursive():
    r = Redactor(["supersecretvalue"])
    src = {"a": ["supersecretvalue", {"b": "x supersecretvalue"}], "n": 3}
    out = r.redact_obj(src)
    assert out == {"a": ["***", {"b": "x ***"}], "n": 3}
    assert src["a"][0] == "supersecretvalue"


def test_add_extends():
    r = Redactor()
    r.add(["anothersecret"])
    assert r.redact("anothersecret") == "***"


def test_non_str_unchanged():
    r = Redactor(["supersecretvalue"])
    assert r.redact(None) is None
    assert r.redact(5) == 5
    assert r.redact(b"x") == b"x"
