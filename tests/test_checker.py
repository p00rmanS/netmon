from netmon.checker import parse_ping_output


def test_windows_reply():
    out = "Reply from 192.168.50.1: bytes=32 time=1ms TTL=64"
    assert parse_ping_output(out) == 1.0


def test_windows_sub_millisecond():
    out = "Reply from 192.168.50.1: bytes=32 time<1ms TTL=64"
    assert parse_ping_output(out) == 1.0


def test_windows_unreachable_is_failure():
    # Windows exits 0 for this, so we must not treat it as a reply.
    out = "Reply from 192.168.50.82: Destination host unreachable."
    assert parse_ping_output(out) is None


def test_windows_timeout():
    assert parse_ping_output("Request timed out.") is None


def test_linux_reply():
    out = "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=3.52 ms"
    assert parse_ping_output(out) == 3.52


def test_macos_reply():
    out = "64 bytes from 192.168.1.1: icmp_seq=0 ttl=64 time=2.104 ms"
    assert parse_ping_output(out) == 2.104


def test_german_locale_reply():
    out = "Antwort von 192.168.50.1: Bytes=32 Zeit=4ms TTL=64"
    assert parse_ping_output(out) == 4.0
