import logging

from halo_server.log_redaction import RedactingFormatter, redact_text
from halo_server.logging_setup import configure_logging


def test_redact_text_removes_url_credentials_query_and_fragment():
    redacted = redact_text(
        "open https://user:pass@[2001:db8::1]:443/video?sig=secret#access-token now"
    )

    assert redacted == "open https://[2001:db8::1]:443/video?<redacted>#<redacted> now"


def test_redact_text_removes_websocket_and_rtsp_credentials():
    redacted = redact_text(
        "connect wss://user:secret@example.test/events?token=value and "
        "rtsp://halo:camera-secret@127.0.0.1:18554/halo/opaque"
    )

    assert redacted == (
        "connect wss://example.test/events?<redacted> and rtsp://127.0.0.1:18554/halo/opaque"
    )


def test_redact_text_removes_ring_and_plugin_secret_fields():
    redacted = redact_text(
        """{"refresh_token": "ring-secret", "plugin_secret": "plugin-secret"} """
        "pairing_code=pair-secret Authorization: Bearer auth-secret"
    )

    for secret in ("ring-secret", "plugin-secret", "pair-secret", "auth-secret"):
        assert secret not in redacted
    assert redacted.count("<redacted>") == 4


def test_redacting_formatter_sanitizes_rendered_log_arguments():
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    record = logging.LogRecord(
        "halo_server.test",
        logging.WARNING,
        __file__,
        1,
        "request failed: %s",
        ("https://example.test/path?oauth_token=secret",),
        None,
    )

    rendered = formatter.format(record)

    assert rendered == "WARNING request failed: https://example.test/path?<redacted>"


def test_server_logging_configuration_installs_only_redacting_handlers():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    try:
        configure_logging("WARNING")

        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, RedactingFormatter)
    finally:
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_redact_homekit_reveal_and_transport_key_spellings():
    value = (
        "PIN 031-45-154 X-HM://0023ISYWYABCD "
        '{"pincode":"canary-pin", "setupURI":"canary-uri", '
        '"signSk":"canary-sign", "srtp_key":"canary-key", "srtpSalt":"canary-salt", '
        '"private_key":"canary-private"} paired_clients=2 state=ready codec=h264'
    )
    result = redact_text(value)
    assert "canary-" not in result and "031-45-154" not in result and "X-HM://" not in result
    assert "paired_clients=2 state=ready codec=h264" in result


def test_redacting_formatter_sanitizes_exception_and_multiline_reveal():
    try:
        raise RuntimeError("failed setupURI=X-HM://0023ISYWYABCD\npincode=031-45-154")
    except RuntimeError:
        import sys

        record = logging.LogRecord(
            "halo",
            logging.ERROR,
            __file__,
            1,
            "failed %s",
            ("srtp_key=key-canary",),
            sys.exc_info(),
        )
    result = RedactingFormatter().format(record)
    assert "key-canary" not in result and "031-45-154" not in result and "X-HM://" not in result
    assert "RuntimeError" in result


def test_redaction_hides_multiline_quoted_secret_without_hiding_adjacent_status():
    result = redact_text('private_key="first-canary\nsecond-canary" paired_clients=2')
    assert "canary" not in result and "paired_clients=2" in result
