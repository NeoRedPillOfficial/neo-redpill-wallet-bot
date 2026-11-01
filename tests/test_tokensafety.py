from bot import tokensafety as ts


def test_a_token_sent_by_its_own_contract_is_spam():
    a = "0xB95814be939f284187fd6920d70cb8ef11d18dc0"
    assert ts.sent_by_itself(a.lower(), a)
    assert not ts.sent_by_itself("0x" + "1" * 40, a)
    assert not ts.sent_by_itself(None, a)


def test_honeypots_and_heavy_taxes_are_refused_and_light_ones_warned():
    refuse, _ = ts.judge({"honeypotResult": {"isHoneypot": True, "honeypotReason": "cannot sell"}})
    assert refuse and "honeypot" in refuse
    refuse, _ = ts.judge({"honeypotResult": {"isHoneypot": False}, "simulationResult": {"buyTax": 2, "sellTax": 90}})
    assert refuse and "90%" in refuse
    refuse, warn = ts.judge({"honeypotResult": {"isHoneypot": False}, "simulationResult": {"buyTax": 0, "sellTax": 15.5}})
    assert refuse is None and any("16%" in w or "15%" in w for w in warn)
    assert ts.judge({"honeypotResult": {"isHoneypot": False}, "simulationResult": {"buyTax": 0, "sellTax": 0}}) == (None, [])


def test_missing_or_strange_answers_never_refuse_a_token():
    for junk in (None, [], "x", {}, {"honeypotResult": None}, {"simulationResult": {"sellTax": "abc"}}):
        refuse, warn = ts.judge(junk)
        assert refuse is None
        assert warn  # the person is told it could not be checked
