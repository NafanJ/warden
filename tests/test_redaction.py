from warden.agent.report import redact, write_incident_report


def test_secrets_replaced():
    out = redact("key=sonarr-secret-key-123 pass=hunter2", ["sonarr-secret-key-123", "hunter2"])
    assert "sonarr-secret-key-123" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_lan_ips_replaced():
    out = redact("host at 192.168.0.101 and 10.0.1.5 and 172.16.4.20", [])
    assert "192.168.0.101" not in out
    assert "10.0.1.5" not in out
    assert "172.16.4.20" not in out
    assert out.count("[LAN-IP]") == 3


def test_public_ips_kept():
    out = redact("cloudflare at 104.16.132.229", [])
    assert "104.16.132.229" in out


def test_key_like_strings_replaced():
    out = redact("token 4370d649f0204c11be21571aefa84174 in log", [])
    assert "4370d649f0204c11be21571aefa84174" not in out


def test_report_file_is_redacted(config):
    md = "## Observed\nSonarr key sonarr-secret-key-123 at 192.168.0.101 failed auth hunter2-password"
    path = write_incident_report(config, 7, "Test incident", "container_down", "resolved", md)
    content = path.read_text()
    assert "sonarr-secret-key-123" not in content
    assert "hunter2-password" not in content
    assert "192.168.0.101" not in content
    assert "Test incident" in content
