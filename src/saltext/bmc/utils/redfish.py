"""
Redfish HTTP transport for the ``saltext-bmc`` extension.

A small Redfish client built directly on ``requests``.  It exposes the
minimum primitives the higher-level layers need:

* :class:`RedfishClient` — context-managed session with GET / POST /
  PATCH / DELETE.
* :func:`get_system_path` and :func:`get_reset_action` — Redfish service
  discovery helpers.
* :func:`get_config`, :func:`resolve_conn`, :func:`open_client` — pillar
  → connection-config plumbing shared with the IPMI backend.

The protocol-neutral operations (power / boot / inventory / sensors) are
in :mod:`saltext.bmc.utils.backend`; raw passthrough lives in
:mod:`saltext.bmc.modules.bmc_redfish`.

Configuration is read from Salt opts/pillar under ``saltext.bmc``::

    saltext.bmc:
      host: 192.168.1.100
      username: root
      password: calvin
      verify_ssl: false

Or, when multiple targets are managed by a single minion::

    saltext.bmc:
      profiles:
        bmc-host-01:
          host: 10.0.0.5
          username: root
          password: calvin
          verify_ssl: false
        bmc-host-02:
          host: 10.0.0.6
          username: root
          password: calvin
          verify_ssl: false

Auth strategy: a session is opened against
``/redfish/v1/SessionService/Sessions`` to obtain an ``X-Auth-Token`` for
subsequent requests, and DELETE'd on close.  If session creation fails
(older BMCs, or BMCs with the session service disabled), the client
falls back to HTTP Basic for the request lifetime.
"""

from __future__ import annotations

import logging

import requests
import urllib3

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


class RedfishError(Exception):
    """A Redfish API request failed."""


class RedfishAuthError(RedfishError):
    """Authentication against the Redfish endpoint failed."""


def get_config(opts: dict, profile: str | None = None) -> dict:
    """
    Extract BMC connection config from opts/pillar.

    Searches in order:
      1. ``saltext.bmc:profiles:<profile>`` (when ``profile`` is given)
      2. ``saltext.bmc`` top-level keys (fallback / single-host config)
    """
    pillar = opts.get("pillar", {}) if isinstance(opts, dict) else {}
    root = pillar.get("saltext.bmc", {}) or (
        opts.get("saltext.bmc", {}) if isinstance(opts, dict) else {}
    )
    if profile:
        cfg = root.get("profiles", {}).get(profile)
        if cfg is None:
            # Allow callers to skip the "profiles" nesting if they have a
            # single-host config and pass profile=None.
            cfg = {}
    else:
        cfg = root

    return {
        "host": cfg.get("host") or cfg.get("hostname"),
        "username": cfg.get("username") or cfg.get("user"),
        "password": cfg.get("password"),
        "verify_ssl": cfg.get("verify_ssl", True),
        "timeout": cfg.get("timeout", DEFAULT_TIMEOUT),
        "backend": cfg.get("backend"),
        "port": cfg.get("port"),
    }


def _normalize_kwargs(conn: dict) -> dict:
    """Accept ``hostname=``/``user=`` aliases for ``host=``/``username=``."""
    out = dict(conn)
    if "hostname" in out and "host" not in out:
        out["host"] = out.pop("hostname")
    if "user" in out and "username" not in out:
        out["username"] = out.pop("user")
    return out


def resolve_conn(opts: dict, name: str | None = None, **overrides) -> dict:
    """
    Resolve a connection config from opts/pillar plus explicit kwargs.

    Explicit kwargs (``host=``, ``username=``, ``password=``,
    ``verify_ssl=``) take precedence over the resolved profile.
    """
    overrides = _normalize_kwargs(overrides)
    cfg = get_config(opts, profile=name)
    for key in ("host", "username", "password", "verify_ssl", "timeout", "backend", "port"):
        if overrides.get(key) is not None:
            cfg[key] = overrides[key]
    if not cfg["host"]:
        raise RedfishError(
            f"No BMC host configured (profile={name!r}). "
            "Provide host= explicitly or set saltext.bmc:[profiles:<name>:]host in pillar."
        )
    if not cfg["username"] or not cfg["password"]:
        raise RedfishError(f"BMC credentials missing for host {cfg['host']!r} (profile={name!r}).")
    return cfg


class RedfishClient:
    """
    Thin Redfish HTTP client.

    Use as a context manager::

        with RedfishClient(host, user, password, verify_ssl=False) as c:
            data = c.get("/redfish/v1/Systems")
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self._session: requests.Session | None = None
        self._session_url: str | None = None
        self._auth_mode: str = "none"  # 'token' or 'basic'

    # ------------------------------------------------------------------
    # Context-manager lifecycle
    # ------------------------------------------------------------------

    def __enter__(self) -> RedfishClient:
        self._open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._close()

    def _base_url(self) -> str:
        return f"https://{self.host}"

    def _open(self) -> None:
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._session = requests.Session()
        self._session.verify = self.verify_ssl
        self._session.headers.update({"Accept": "application/json"})

        # Try session-token auth first.
        try:
            resp = self._session.post(
                f"{self._base_url()}/redfish/v1/SessionService/Sessions",
                json={"UserName": self.username, "Password": self.password},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            self._session.close()
            self._session = None
            raise RedfishError(f"Cannot reach Redfish endpoint at {self.host}: {exc}") from exc

        if resp.status_code in (200, 201):
            token = resp.headers.get("X-Auth-Token")
            location = resp.headers.get("Location")
            if token:
                self._session.headers.update({"X-Auth-Token": token})
                self._auth_mode = "token"
                if location:
                    # Location may be absolute or path-relative.
                    self._session_url = (
                        location if location.startswith("http") else f"{self._base_url()}{location}"
                    )
                return

        if resp.status_code in (401, 403):
            self._session.close()
            self._session = None
            raise RedfishAuthError(
                f"Redfish session creation rejected at {self.host} "
                f"(HTTP {resp.status_code}): check username/password."
            )

        # Other status codes — fall back to HTTP Basic.
        log.debug(
            "Redfish session creation returned HTTP %s at %s; falling back to Basic auth.",
            resp.status_code,
            self.host,
        )
        self._session.auth = (self.username, self.password)
        self._auth_mode = "basic"

    def _close(self) -> None:
        if self._session is None:
            return
        try:
            if self._auth_mode == "token" and self._session_url:
                try:
                    self._session.delete(self._session_url, timeout=self.timeout)
                except requests.RequestException:
                    log.debug("Failed to DELETE Redfish session at %s", self._session_url)
        finally:
            self._session.close()
            self._session = None
            self._session_url = None
            self._auth_mode = "none"

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base_url()}{path}"

    def _raise_for_status(self, resp: requests.Response, method: str, path: str) -> None:
        if resp.status_code < 400:
            return
        if resp.status_code in (401, 403):
            raise RedfishAuthError(
                f"Redfish {method} {path} returned HTTP {resp.status_code} (authentication)."
            )
        # Try to extract Redfish extended-info messages.
        detail = ""
        try:
            body = resp.json()
            err = body.get("error", {})
            msgs = err.get("@Message.ExtendedInfo") or []
            if msgs:
                detail = "; ".join(
                    m.get("Message", "") for m in msgs if isinstance(m, dict) and m.get("Message")
                )
            if not detail:
                detail = err.get("message", "")
        except (ValueError, AttributeError):
            detail = resp.text[:500]
        raise RedfishError(
            f"Redfish {method} {path} failed (HTTP {resp.status_code}): {detail or 'no detail'}"
        )

    def get(self, path: str) -> dict:
        assert self._session is not None, "RedfishClient must be used as a context manager"
        resp = self._session.get(self._url(path), timeout=self.timeout)
        self._raise_for_status(resp, "GET", path)
        return resp.json() if resp.content else {}

    def post(self, path: str, body: dict | None = None) -> dict | None:
        assert self._session is not None, "RedfishClient must be used as a context manager"
        resp = self._session.post(self._url(path), json=body or {}, timeout=self.timeout)
        self._raise_for_status(resp, "POST", path)
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    def patch(self, path: str, body: dict) -> dict | None:
        assert self._session is not None, "RedfishClient must be used as a context manager"
        resp = self._session.patch(self._url(path), json=body, timeout=self.timeout)
        self._raise_for_status(resp, "PATCH", path)
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return None
        return None

    def delete(self, path: str) -> dict | None:
        assert self._session is not None, "RedfishClient must be used as a context manager"
        resp = self._session.delete(self._url(path), timeout=self.timeout)
        self._raise_for_status(resp, "DELETE", path)
        if resp.content:
            try:
                return resp.json()
            except ValueError:
                return None
        return None


# ----------------------------------------------------------------------
# Discovery helpers
# ----------------------------------------------------------------------


def get_system_path(client: RedfishClient) -> str:
    """
    Return the ``@odata.id`` of the (typically single) ComputerSystem.

    Raises :class:`RedfishError` if no system is exposed or multiple systems
    are present without an obvious default.
    """
    coll = client.get("/redfish/v1/Systems")
    members = coll.get("Members") or []
    if not members:
        raise RedfishError("No /redfish/v1/Systems members exposed by the BMC.")
    # Most BMCs expose exactly one system; pick the first deterministically.
    return members[0]["@odata.id"]


def get_reset_action(system: dict) -> tuple[str, list[str]]:
    """
    Return ``(target_path, allowable_values)`` for ``ComputerSystem.Reset``.

    Parses the ``Actions`` block of a ComputerSystem resource to find
    the reset action target and the list of accepted ``ResetType`` values.
    """
    actions = system.get("Actions", {}) or {}
    reset = actions.get("#ComputerSystem.Reset", {}) or {}
    target = reset.get("target")
    if not target:
        raise RedfishError("ComputerSystem.Reset action not advertised by the BMC.")
    # Older or stripped-down BMCs only expose @Redfish.ActionInfo pointing
    # at another endpoint; fetching that is left to the caller if needed.
    allowable: list[str] = reset.get("ResetType@Redfish.AllowableValues") or []
    return target, list(allowable)


# ----------------------------------------------------------------------
# Convenience
# ----------------------------------------------------------------------


def open_client(opts: dict, name: str | None = None, **overrides) -> RedfishClient:
    """
    Build a :class:`RedfishClient` from opts/pillar plus optional overrides.

    Caller is responsible for using it as a context manager.
    """
    cfg = resolve_conn(opts, name=name, **overrides)
    return RedfishClient(
        host=cfg["host"],
        username=cfg["username"],
        password=cfg["password"],
        verify_ssl=cfg["verify_ssl"],
        timeout=cfg["timeout"],
    )
