"""Tests for erza.security.network — SSRF protection and internal URL detection."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from erza.security.network import (
    configure_ssrf_whitelist,
    contains_internal_url,
    validate_url_target,
)


def _fake_resolve(host: str, results: list[str]):
    """Return a getaddrinfo mock that maps the given host to fake IP results."""

    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")

    return _resolver


# ---------------------------------------------------------------------------
# validate_url_target — scheme / domain basics
# ---------------------------------------------------------------------------


def test_rejects_non_http_scheme():
    ok, err = validate_url_target("ftp://example.com/file")
    assert not ok
    assert "http" in err.lower()


def test_rejects_missing_domain():
    ok, err = validate_url_target("http://")
    assert not ok


# ---------------------------------------------------------------------------
# validate_url_target — blocked private/internal IPs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip,label",
    [
        ("127.0.0.1", "loopback"),
        ("127.0.0.2", "loopback_alt"),
        ("10.0.0.1", "rfc1918_10"),
        ("172.16.5.1", "rfc1918_172"),
        ("192.168.1.1", "rfc1918_192"),
        ("169.254.169.254", "metadata"),
        ("0.0.0.0", "zero"),
    ],
)
def test_blocks_private_ipv4(ip: str, label: str):
    with patch("erza.security.network.socket.getaddrinfo", _fake_resolve("evil.com", [ip])):
        ok, err = validate_url_target("http://evil.com/path")
        assert not ok, f"Should block {label} ({ip})"
        assert "private" in err.lower() or "blocked" in err.lower()


def test_blocks_ipv6_loopback():
    def _resolver(hostname, port, family=0, type_=0):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 0, 0, 0))]

    with patch("erza.security.network.socket.getaddrinfo", _resolver):
        ok, err = validate_url_target("http://evil.com/")
        assert not ok


# ---------------------------------------------------------------------------
# validate_url_target — IPv6-mapped IPv4 bypass prevention
# ---------------------------------------------------------------------------


def _fake_resolve_v6(host: str, results: list[str]):
    """Like _fake_resolve but returns AF_INET6 tuples for IPv6 addresses."""

    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            entries = []
            for ip in results:
                if ":" in ip:
                    entries.append((socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ip, 0, 0, 0)))
                else:
                    entries.append((socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)))
            return entries
        raise socket.gaierror(f"cannot resolve {hostname}")

    return _resolver


def test_blocks_ipv6_mapped_loopback():
    """::ffff:127.0.0.1 must be blocked just like 127.0.0.1."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve_v6("evil.com", ["::ffff:127.0.0.1"]),
    ):
        ok, err = validate_url_target("http://evil.com/")
        assert not ok
        assert "blocked" in err.lower()


def test_blocks_ipv6_mapped_metadata():
    """::ffff:169.254.169.254 must be blocked just like 169.254.169.254."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve_v6("evil.com", ["::ffff:169.254.169.254"]),
    ):
        ok, err = validate_url_target("http://evil.com/")
        assert not ok


def test_blocks_ipv6_mapped_rfc1918():
    """::ffff:10.0.0.1 must be blocked just like 10.0.0.1."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve_v6("evil.com", ["::ffff:10.0.0.1"]),
    ):
        ok, err = validate_url_target("http://evil.com/")
        assert not ok


def test_allows_public_ipv6():
    """Public IPv6 addresses must still be allowed."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve_v6("example.com", ["2606:4700::6810:84e5"]),
    ):
        ok, err = validate_url_target("http://example.com/")
        assert ok, f"Should allow public IPv6, got: {err}"


# ---------------------------------------------------------------------------
# validate_url_target — allows public IPs
# ---------------------------------------------------------------------------


def test_allows_public_ip():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["93.184.216.34"]),
    ):
        ok, err = validate_url_target("http://example.com/page")
        assert ok, f"Should allow public IP, got: {err}"


def test_allows_normal_https():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("github.com", ["140.82.121.3"]),
    ):
        ok, err = validate_url_target("https://github.com/HKUDS/erza")
        assert ok


# ---------------------------------------------------------------------------
# contains_internal_url — shell command scanning
# ---------------------------------------------------------------------------


def test_detects_curl_metadata():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        assert contains_internal_url("curl -s http://169.254.169.254/computeMetadata/v1/")


def test_detects_wget_localhost():
    with patch(
        "erza.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])
    ):
        assert contains_internal_url("wget http://localhost:8080/secret")


def test_loopback_exception_allows_literal_localhost_only():
    with patch(
        "erza.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])
    ):
        assert not contains_internal_url("curl http://localhost:8765/", allow_loopback=True)


def test_loopback_exception_rejects_public_name_resolving_to_loopback():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["127.0.0.1"]),
    ):
        assert contains_internal_url("curl http://example.com:8765/", allow_loopback=True)


def test_loopback_exception_rejects_metadata():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        assert contains_internal_url(
            "curl http://169.254.169.254/latest/meta-data/", allow_loopback=True
        )


def test_detects_ipv6_mapped_loopback():
    """contains_internal_url must catch IPv6-mapped loopback in shell commands."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve_v6("evil.com", ["::ffff:127.0.0.1"]),
    ):
        assert contains_internal_url("curl http://evil.com/secret")


def test_allows_normal_curl():
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["93.184.216.34"]),
    ):
        assert not contains_internal_url("curl https://example.com/api/data")


def test_no_urls_returns_false():
    assert not contains_internal_url("echo hello && ls -la")


# ---------------------------------------------------------------------------
# schemeless curl/wget URL detection (SSRF bypass prevention)
# ---------------------------------------------------------------------------


def test_detects_schemeless_curl_metadata():
    """``curl 169.254.169.254`` (no scheme) must be caught — curl defaults to http://."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        assert contains_internal_url("curl -s 169.254.169.254/computeMetadata/v1/")


def test_detects_schemeless_wget_localhost():
    """``wget localhost:8080`` (no scheme) must be caught — wget defaults to http://."""
    with patch(
        "erza.security.network.socket.getaddrinfo", _fake_resolve("localhost", ["127.0.0.1"])
    ):
        assert contains_internal_url("wget localhost:8080/secret")


def test_detects_schemeless_curl_domain_resolving_to_internal():
    """``curl example.com`` resolving to a private IP must be caught."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["10.0.0.5"]),
    ):
        assert contains_internal_url("curl example.com/api")


def test_schemeless_curl_public_domain_allowed():
    """``curl example.com`` resolving to a public IP is allowed."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["93.184.216.34"]),
    ):
        assert not contains_internal_url("curl example.com/api/data")


def test_schemeless_not_triggered_for_non_http_clients():
    """``git config user.name`` must not be flagged as a schemeless URL."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("example.com", ["127.0.0.1"]),
    ):
        # 'git config user.email' contains 'user.email' which looks domain-like,
        # but since git is not an HTTP client, it must not trigger SSRF check.
        assert not contains_internal_url("git config user.email a@b.com")


def test_schemeless_curl_exe_detected():
    """``curl.exe`` on Windows must also be detected."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("169.254.169.254", ["169.254.169.254"]),
    ):
        assert contains_internal_url("curl.exe -s 169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# SSRF whitelist — allow specific CIDR ranges (#2669)
# ---------------------------------------------------------------------------


def test_blocks_cgnat_by_default():
    """100.64.0.0/10 (CGNAT / Tailscale) is blocked by default."""
    with patch(
        "erza.security.network.socket.getaddrinfo",
        _fake_resolve("ts.local", ["100.100.1.1"]),
    ):
        ok, _ = validate_url_target("http://ts.local/api")
        assert not ok


def test_whitelist_allows_cgnat():
    """Whitelisting 100.64.0.0/10 lets Tailscale addresses through."""
    configure_ssrf_whitelist(["100.64.0.0/10"])
    try:
        with patch(
            "erza.security.network.socket.getaddrinfo",
            _fake_resolve("ts.local", ["100.100.1.1"]),
        ):
            ok, err = validate_url_target("http://ts.local/api")
            assert ok, f"Whitelisted CGNAT should be allowed, got: {err}"
    finally:
        configure_ssrf_whitelist([])


def test_whitelist_does_not_affect_other_blocked():
    """Whitelisting CGNAT must not unblock other private ranges."""
    configure_ssrf_whitelist(["100.64.0.0/10"])
    try:
        with patch(
            "erza.security.network.socket.getaddrinfo",
            _fake_resolve("evil.com", ["10.0.0.1"]),
        ):
            ok, _ = validate_url_target("http://evil.com/secret")
            assert not ok
    finally:
        configure_ssrf_whitelist([])


def test_whitelist_invalid_cidr_ignored():
    """Invalid CIDR entries are silently skipped."""
    configure_ssrf_whitelist(["not-a-cidr", "100.64.0.0/10"])
    try:
        with patch(
            "erza.security.network.socket.getaddrinfo",
            _fake_resolve("ts.local", ["100.100.1.1"]),
        ):
            ok, _ = validate_url_target("http://ts.local/api")
            assert ok
    finally:
        configure_ssrf_whitelist([])


def test_whitelist_allows_ipv6_mapped_cgnat():
    """Whitelist must work when DNS returns IPv6-mapped CGNAT address."""
    configure_ssrf_whitelist(["100.64.0.0/10"])
    try:
        with patch(
            "erza.security.network.socket.getaddrinfo",
            _fake_resolve_v6("ts.local", ["::ffff:100.100.1.1"]),
        ):
            ok, err = validate_url_target("http://ts.local/api")
            assert ok, f"Whitelisted IPv6-mapped CGNAT should be allowed, got: {err}"
    finally:
        configure_ssrf_whitelist([])


# ---------------------------------------------------------------------------
# SSRF 回归：IPv6 特殊网段与"遗留数字形式"绕过
#
# 历史缺陷：
#   * ``_BLOCKED_NETWORKS`` 缺 ``::/128`` / ``::/96`` / ``64:ff9b::/96`` /
#     ``2002::/16``，导致 ``http://[::]/`` 与 NAT64 地址被放行；
#   * ``_URL_RE`` 只匹配 http(s)，``curl gopher://169.254.169.254/_`` 完全
#     绕过 exec 的命令行守卫；
#   * schemeless 提取只认点分四段 IPv4，漏掉十进制/十六进制/八进制/短式。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://[::]/",
        "http://[0:0:0:0:0:0:0:0]/",
        "http://[64:ff9b::127.0.0.1]/",
        "http://[64:ff9b::a9fe:a9fe]/",
        "http://[2002:7f00:0001::]/",
        "http://0.0.0.0/",
    ],
)
def test_ipv6_special_networks_are_blocked(url):
    ok, err = validate_url_target(url)
    assert not ok, f"{url} 应被拦截,实际放行(err={err!r})"


def test_nat64_and_unspecified_stay_blocked_even_when_whitelisted():
    """``::/128`` 与 NAT64 属于 hard-block，操作员白名单不得放行。"""
    configure_ssrf_whitelist(["::/0", "64:ff9b::/96", "0.0.0.0/8"])
    try:
        for url in ("http://[::]/", "http://[64:ff9b::127.0.0.1]/", "http://0.0.0.0/"):
            ok, _ = validate_url_target(url)
            assert not ok, f"{url} 不应被白名单放行"
    finally:
        configure_ssrf_whitelist([])


@pytest.mark.parametrize(
    "command",
    [
        "curl gopher://169.254.169.254/_",
        "curl ftp://127.0.0.1/",
        "wget dict://127.0.0.1:11211/stat",
        "curl ldap://169.254.169.254/",
    ],
)
def test_non_http_scheme_targets_are_detected(command):
    """非 http(s) scheme 的内部目标必须被识别(exec 场景下这是唯一防线)。"""
    assert contains_internal_url(command), f"{command} 未被识别为内网目标"


@pytest.mark.parametrize(
    "target",
    [
        "2130706433",  # 打包十进制 127.0.0.1
        "0x7f000001",  # 十六进制
        "127.1",  # 短式
        "0177.0.0.1",  # 八进制
        "2852039166",  # 打包十进制 169.254.169.254
    ],
)
def test_legacy_numeric_ip_forms_are_detected(target):
    assert contains_internal_url(f"curl {target}"), f"curl {target} 未被拦截"
    assert contains_internal_url(f"wget {target}"), f"wget {target} 未被拦截"


@pytest.mark.parametrize(
    "command",
    [
        "curl https://example.com/api",
        "curl -s https://api.github.com/repos",
        "curl --retry 5 https://example.com",
        "git config user.email",
        "git log --oneline -5",
        "npm run build",
        "echo 2>&1",
        "pytest -q",
    ],
)
def test_public_and_unrelated_commands_are_not_flagged(command):
    """新增的数字形式与任意-scheme 匹配不得引入误报。"""
    assert not contains_internal_url(command), f"{command} 被误判为内网目标"
