"""TLS settings for every outgoing HTTPS call (RPC nodes, price APIs).

Python normally trusts only the operating system's certificate store. A freshly installed Windows
Server often lacks some root certificates, and then every call to such a site fails with
"CERTIFICATE_VERIFY_FAILED" even though the site is fine (PowerShell and browsers still work because
Windows fetches the missing certificates on its own; Python doesn't). So we trust the system store AND
the standard Mozilla root list shipped in the `certifi` package. Verification stays fully on.
"""
import ssl


def context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi

        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:  # certifi missing: fall back to the system store alone
        pass
    return ctx
