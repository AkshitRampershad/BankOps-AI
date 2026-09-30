from cua.redaction import Redactor


def test_patterns():
    r = Redactor()
    s = "SSN 123-45-6789 phone (555) 201-4432 mail a.b@x.org card 4111 1111 1111 1111 acct 1234567890123"
    out = r.text(s)
    assert "123-45-6789" not in out and "«ssn»" in out
    assert "201-4432" not in out and "«phone»" in out
    assert "a.b@x.org" not in out
    assert "4111" not in out and "«pan»" in out
    # 13 digits that fail Luhn are not a card number
    assert "1234567890123" in out


def test_registered_values_and_deep():
    r = Redactor()
    r.register("100234", "pii:member_id")
    r.register("s3cret-pw", "secret")
    r.register("ab", "too-short")  # ignored: would redact noise
    obj = {"a": ["member 100234", {"b": "pw=s3cret-pw"}], "n": 5, "c": "cab"}
    out = r.obj(obj)
    assert out == {"a": ["member «pii:member_id»", {"b": "pw=«secret»"}], "n": 5, "c": "cab"}
    assert r.contains_sensitive("x 100234") and not r.contains_sensitive("nothing here")


def test_label_adjacent_values():
    r = Redactor()
    text = "MEMBER PROFILE\nName:\tJANE Q SAMPLE\nSSN:\t123-45-6789\nSuffix\tBalance\nS0001\t1,234.56"
    out = r.labeled(text, ["Name:", "SSN:"])
    assert "JANE" not in out and "123-45" not in out
    assert "Name: «pii»" in out and "1,234.56" in out
