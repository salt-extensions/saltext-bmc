import logging
import os

import pytest
import responses as responses_lib

from saltext.bmc import PACKAGE_ROOT

logging.root.setLevel(logging.WARNING)

for handler in logging.root.handlers[:]:  # pragma: no cover
    logging.root.removeHandler(handler)
    handler.close()


@pytest.fixture(scope="session")
def salt_factories_config():  # pragma: no cover
    return {
        "code_dir": str(PACKAGE_ROOT),
        "inject_sitecustomize": "COVERAGE_PROCESS_START" in os.environ,
        "start_timeout": 120 if os.environ.get("CI") else 60,
    }


# ---------------------------------------------------------------------------
# Redfish HTTP-mock fixtures
# ---------------------------------------------------------------------------

REDFISH_HOST = "bmc.test"
REDFISH_USER = "root"
REDFISH_PASS = "calvin"
REDFISH_SYS_ID = "1"
REDFISH_BASE = f"https://{REDFISH_HOST}"
REDFISH_SYS_PATH = f"/redfish/v1/Systems/{REDFISH_SYS_ID}"
REDFISH_RESET_PATH = f"{REDFISH_SYS_PATH}/Actions/ComputerSystem.Reset"


@pytest.fixture
def bmc_opts():
    """Salt opts/pillar dict with a single profile pointing at the mock host.

    Pins ``backend: redfish`` so tests don't trigger the live HTTPS probe that
    runs when no backend is declared (now ``auto``).  IPMI-specific tests
    override this in their own setup.
    """
    return {
        "pillar": {
            "saltext.bmc": {
                "profiles": {
                    "test-host": {
                        "host": REDFISH_HOST,
                        "username": REDFISH_USER,
                        "password": REDFISH_PASS,
                        "verify_ssl": False,
                        "backend": "redfish",
                    }
                }
            }
        }
    }


def _system_doc(power_state="On", boot_target="None", boot_enabled="Disabled"):
    return {
        "@odata.id": REDFISH_SYS_PATH,
        "PowerState": power_state,
        "Boot": {
            "BootSourceOverrideTarget": boot_target,
            "BootSourceOverrideEnabled": boot_enabled,
        },
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": REDFISH_RESET_PATH,
                "ResetType@Redfish.AllowableValues": [
                    "On",
                    "ForceOff",
                    "GracefulShutdown",
                    "GracefulRestart",
                    "ForceRestart",
                    "PowerCycle",
                ],
            }
        },
    }


def _register_session(rsps):
    """Register the Redfish session-creation endpoint."""
    rsps.add(
        responses_lib.POST,
        f"{REDFISH_BASE}/redfish/v1/SessionService/Sessions",
        json={"Id": "1", "UserName": REDFISH_USER},
        status=201,
        headers={
            "X-Auth-Token": "test-token-xyz",
            "Location": "/redfish/v1/SessionService/Sessions/1",
        },
    )
    rsps.add(
        responses_lib.DELETE,
        f"{REDFISH_BASE}/redfish/v1/SessionService/Sessions/1",
        status=200,
    )


def _register_systems(rsps, system=None):
    rsps.add(
        responses_lib.GET,
        f"{REDFISH_BASE}/redfish/v1/Systems",
        json={"Members": [{"@odata.id": REDFISH_SYS_PATH}]},
        status=200,
    )
    rsps.add(
        responses_lib.GET,
        f"{REDFISH_BASE}{REDFISH_SYS_PATH}",
        json=system or _system_doc(),
        status=200,
    )


@pytest.fixture
def mocked_redfish():
    """Activate ``responses`` with the Redfish session endpoints pre-registered."""
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _register_session(rsps)
        yield rsps


@pytest.fixture
def mocked_redfish_full(mocked_redfish):
    """``mocked_redfish`` plus a /redfish/v1/Systems collection and a system doc."""
    _register_systems(mocked_redfish)
    return mocked_redfish


@pytest.fixture
def system_doc():
    """Factory returning a customisable system document."""
    return _system_doc


@pytest.fixture
def register_systems():
    """Expose the systems-registration helper to tests."""
    return _register_systems
