import ssl

from bot import tls


def test_context_verifies_certificates_and_adds_the_certifi_roots():
    ctx = tls.context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    try:
        import certifi  # noqa: F401
    except ImportError:
        return
    assert ctx.cert_store_stats()["x509_ca"] >= 100  # a real root list was loaded, not an empty store
