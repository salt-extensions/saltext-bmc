import pytest
import responses

from saltext.bmc.utils import redfish as rf
from tests.conftest import REDFISH_BASE
from tests.conftest import REDFISH_HOST
from tests.conftest import REDFISH_PASS
from tests.conftest import REDFISH_SYS_PATH
from tests.conftest import REDFISH_USER


def _client():
    return rf.RedfishClient(
        host=REDFISH_HOST,
        username=REDFISH_USER,
        password=REDFISH_PASS,
        verify_ssl=False,
    )


def test_client_uses_token_auth_when_session_endpoint_works(mocked_redfish):
    mocked_redfish.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": REDFISH_SYS_PATH}]},
        status=200,
    )

    with _client() as client:
        result = client.get("/redfish/v1/Systems")
        assert "Members" in result
        # X-Auth-Token must be attached to subsequent requests.
        sent = [c.request for c in mocked_redfish.calls if "/Systems" in c.request.url][0]
        assert sent.headers.get("X-Auth-Token") == "test-token-xyz"


def test_client_falls_back_to_basic_auth_when_session_unsupported():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.POST,
            f"{REDFISH_BASE}/redfish/v1/SessionService/Sessions",
            json={"error": {"message": "not supported"}},
            status=404,
        )
        rsps.add(
            responses.GET,
            f"{REDFISH_BASE}/redfish/v1/Systems",
            json={"Members": []},
            status=200,
        )

        with _client() as client:
            client.get("/redfish/v1/Systems")
            sent = [c.request for c in rsps.calls if "/Systems" in c.request.url][0]
            # Falls back to Basic auth header.
            assert sent.headers.get("Authorization", "").startswith("Basic ")


def test_client_raises_auth_error_on_401():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses.POST,
            f"{REDFISH_BASE}/redfish/v1/SessionService/Sessions",
            json={"error": {"message": "bad creds"}},
            status=401,
        )
        with pytest.raises(rf.RedfishAuthError):
            with _client():
                pass


def test_get_raises_with_extended_info(mocked_redfish):
    mocked_redfish.add(
        responses.GET,
        f"{REDFISH_BASE}{REDFISH_SYS_PATH}",
        json={
            "error": {
                "message": "Bad",
                "@Message.ExtendedInfo": [
                    {"Message": "boot target not supported"},
                    {"Message": "use one of: Pxe, Hdd"},
                ],
            }
        },
        status=400,
    )
    with _client() as client:
        with pytest.raises(rf.RedfishError) as ei:
            client.get(REDFISH_SYS_PATH)
        msg = str(ei.value)
        assert "boot target not supported" in msg
        assert "use one of" in msg


def test_get_system_path_picks_first_member(mocked_redfish_full):
    with _client() as client:
        assert rf.get_system_path(client) == REDFISH_SYS_PATH


def test_get_system_path_raises_when_empty(mocked_redfish):
    mocked_redfish.add(
        responses.GET,
        f"{REDFISH_BASE}/redfish/v1/Systems",
        json={"Members": []},
        status=200,
    )
    with _client() as client:
        with pytest.raises(rf.RedfishError):
            rf.get_system_path(client)


def test_resolve_conn_pulls_from_pillar_profile(bmc_opts):
    cfg = rf.resolve_conn(bmc_opts, name="test-host")
    assert cfg["host"] == REDFISH_HOST
    assert cfg["username"] == REDFISH_USER
    assert cfg["password"] == REDFISH_PASS
    assert cfg["verify_ssl"] is False


def test_resolve_conn_overrides_win(bmc_opts):
    cfg = rf.resolve_conn(bmc_opts, name="test-host", host="override.example", username="alice")
    assert cfg["host"] == "override.example"
    assert cfg["username"] == "alice"
    assert cfg["password"] == REDFISH_PASS  # not overridden


def test_resolve_conn_missing_host_raises():
    with pytest.raises(rf.RedfishError):
        rf.resolve_conn({}, name="missing")
