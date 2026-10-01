"""Certificates carry one reserved role name; callers cannot supply one (#1161)."""
from __future__ import annotations

import pytest
from cryptography import x509

import _pki

AID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def ca(tmp_path_factory):
    return _pki.load_or_create_ca(tmp_path_factory.mktemp("ca"))


def _cert(ca, **kw) -> x509.Certificate:
    args = {"agent_id": AID, "hostname": "box", "ip_san": "10.0.0.5"}
    args.update(kw)
    pem, _key = _pki.sign_agent_cert(ca[0], ca[1], **args)
    return x509.load_pem_x509_certificate(pem.encode())


def _dns(cert) -> list:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return san.get_values_for_type(x509.DNSName)


def test_role_names():
    assert _pki.role_name("manager") == "manager.role.llmsys.internal"
    assert _pki.role_name("alarm_engine") == "alarm-engine.role.llmsys.internal"
    assert _pki.role_name("agent", AID) == f"{AID}.agent.role.llmsys.internal"


@pytest.mark.parametrize("role,agent_id", [("agent", ""), ("agent", "a b"), ("agent", "a.b"), ("nope", AID)])
def test_role_name_rejects_bad_input(role, agent_id):
    with pytest.raises(ValueError):
        _pki.role_name(role, agent_id)


def test_agent_cert_carries_only_its_own_role(ca):
    dns = _dns(_cert(ca))
    assert f"{AID}.agent.role.llmsys.internal" in dns
    assert "manager.role.llmsys.internal" not in dns
    assert "alarm-engine.role.llmsys.internal" not in dns


def test_default_role_is_agent(ca):
    assert [n for n in _dns(_cert(ca)) if _pki.in_role_zone(n)] == [f"{AID}.agent.role.llmsys.internal"]


@pytest.mark.parametrize("role,cn,want", [
    ("manager", "llm-systems-manager", "manager.role.llmsys.internal"),
    ("alarm_engine", "llm-systems-alarm-engine", "alarm-engine.role.llmsys.internal"),
])
def test_service_certs_carry_their_role(ca, role, cn, want):
    dns = _dns(_cert(ca, agent_id=cn, role=role))
    assert [n for n in dns if _pki.in_role_zone(n)] == [want]


def test_existing_names_are_kept(ca):
    dns = _dns(_cert(ca, extra_dns_sans=["localhost"]))
    assert {"box", "box.agents.local", "localhost"} <= set(dns)


@pytest.mark.parametrize("field,value", [
    ("hostname", "manager.role.llmsys.internal"),
    ("hostname", "Manager.Role.LLMSYS.internal."),
    ("hostname", "role.llmsys.internal"),
    ("extra_dns_sans", ["alarm-engine.role.llmsys.internal"]),
    ("extra_dns_sans", ["x.agent.role.llmsys.internal"]),
    ("extra_dns_sans", ["*.role.llmsys.internal"]),
    ("extra_dns_sans", ["m*.role.llmsys.internal"]),
    ("hostname", "*.agent.role.llmsys.internal"),
])
def test_reserved_names_are_refused(ca, field, value):
    with pytest.raises(ValueError):
        _cert(ca, **{field: value})


@pytest.mark.parametrize("name,inside", [
    ("manager.role.llmsys.internal", True), ("ROLE.llmsys.internal.", True),
    ("a.b.role.llmsys.internal", True), ("role.llmsys.internal.evil.example", False),
    ("xrole.llmsys.internal", False), ("box", False), ("", False), (None, False),
])
def test_in_role_zone(name, inside):
    assert _pki.in_role_zone(name) is inside


def test_subject_organisation_follows_the_role(ca):
    org = lambda c: c.subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)[0].value
    assert org(_cert(ca)) == "LLM Systems Agent"
    assert org(_cert(ca, agent_id="llm-systems-manager", role="manager")) == "LLM Systems Manager"
    assert org(_cert(ca, agent_id="llm-systems-alarm-engine", role="alarm_engine")) == "LLM Systems Alarm Engine"


def test_a_wildcard_outside_the_zone_is_still_signed(ca):
    assert "*.example.com" in _dns(_cert(ca, extra_dns_sans=["*.example.com"]))
