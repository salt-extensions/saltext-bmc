"""Tests for backend dispatch & auto-fallback in saltext.bmc.utils.backend."""

import sys
from unittest.mock import MagicMock

import pytest
import requests
import responses

from saltext.bmc.utils import backend as bk


@pytest.fixture
def base_opts():
    return {
        "pillar": {
            "saltext.bmc": {
                "profiles": {
                    "rf-host": {
                        "host": "rf.test",
                        "username": "root",
                        "password": "calvin",
                        "verify_ssl": False,
                    },
                    "ipmi-host": {
                        "host": "ipmi.test",
                        "username": "ADMIN",
                        "password": "ADMIN",
                        "verify_ssl": False,
                        "backend": "ipmi",
                    },
                    "auto-host": {
                        "host": "auto.test",
                        "username": "root",
                        "password": "calvin",
                        "verify_ssl": False,
                        "backend": "auto",
                    },
                }
            }
        }
    }


@pytest.fixture
def fake_pyghmi(monkeypatch):
    fake_cmd_cls = MagicMock(name="Command")
    fake_module = MagicMock()
    fake_module.Command = fake_cmd_cls
    fake_pkg = MagicMock()
    fake_pkg.ipmi = MagicMock()
    fake_pkg.ipmi.command = fake_module
    monkeypatch.setitem(sys.modules, "pyghmi", fake_pkg)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi", fake_pkg.ipmi)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi.command", fake_module)
    return fake_cmd_cls


def test_default_backend_is_auto(base_opts):
    """A profile that omits ``backend`` should resolve to ``auto`` so mixed
    hardware fleets work without explicit per-profile config.

    Asserted indirectly: with no backend declared on the ``rf-host`` profile,
    the resolver falls through to the auto-probe path.  Mocking a 200 on
    ``/redfish/v1/`` makes the probe succeed → Redfish backend wins.  An
    implicit ``redfish`` default would also produce a Redfish backend but
    without firing the probe; ``test_default_backend_falls_back_to_ipmi_when_probe_fails``
    is the paired test that disambiguates them.
    """
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            "https://rf.test/redfish/v1/",
            json={"@odata.id": "/redfish/v1/"},
            status=200,
        )
        backend = bk.open_backend(base_opts, name="rf-host")
    assert isinstance(backend, bk.RedfishBackend)


def test_default_backend_falls_back_to_ipmi_when_probe_fails(base_opts, fake_pyghmi):
    """Same as above but with the probe failing — proves IPMI fallback works
    for legacy hardware that has no Redfish endpoint."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            "https://rf.test/redfish/v1/",
            body=requests.exceptions.ConnectionError("no Redfish"),
        )
        backend = bk.open_backend(base_opts, name="rf-host")
    assert isinstance(backend, bk.IpmiBackend)


def test_explicit_ipmi_backend(base_opts, fake_pyghmi):
    backend = bk.open_backend(base_opts, name="ipmi-host")
    assert isinstance(backend, bk.IpmiBackend)
    assert backend.name == "ipmi"


def test_override_backend_via_kwarg(base_opts, fake_pyghmi):
    backend = bk.open_backend(base_opts, name="rf-host", backend="ipmi")
    assert isinstance(backend, bk.IpmiBackend)


def test_auto_picks_redfish_when_probe_succeeds(base_opts):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            "https://auto.test/redfish/v1/",
            json={"Name": "Service Root"},
            status=200,
        )
        backend = bk.open_backend(base_opts, name="auto-host")
        assert isinstance(backend, bk.RedfishBackend)


def test_auto_falls_back_to_ipmi_on_probe_failure(base_opts, fake_pyghmi):
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            "https://auto.test/redfish/v1/",
            status=404,
        )
        backend = bk.open_backend(base_opts, name="auto-host")
        assert isinstance(backend, bk.IpmiBackend)


def test_auto_auth_required_still_picks_redfish(base_opts):
    """A 401 on the probe means Redfish is running — pick it."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.GET,
            "https://auto.test/redfish/v1/",
            status=401,
        )
        backend = bk.open_backend(base_opts, name="auto-host")
        assert isinstance(backend, bk.RedfishBackend)


def test_unknown_backend_rejected(base_opts):
    with pytest.raises(ValueError):
        bk.open_backend(base_opts, name="rf-host", backend="quantum")


def test_redfish_backend_passes_credentials(base_opts):
    backend = bk.open_backend(base_opts, name="rf-host", backend="redfish")
    assert backend._client.host == "rf.test"  # pylint: disable=protected-access
    assert backend._client.username == "root"
    assert backend._client.password == "calvin"
    assert backend._client.verify_ssl is False


def test_ipmi_backend_passes_credentials_and_port(base_opts, fake_pyghmi):
    base_opts["pillar"]["saltext.bmc"]["profiles"]["ipmi-host"]["port"] = 6230
    backend = bk.open_backend(base_opts, name="ipmi-host")
    assert backend._client.host == "ipmi.test"  # pylint: disable=protected-access
    assert backend._client.port == 6230
