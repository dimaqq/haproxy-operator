# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""The haproxy service module."""

import logging
import os
import pwd

# We silence this rule because subprocess call is only for validating the haproxy config
# and no user input is parsed
import subprocess  # nosec B404
from pathlib import Path
from subprocess import CalledProcessError, run  # nosec

from charms.operator_libs_linux.v0 import apt
from charms.operator_libs_linux.v1 import systemd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from state.charm_state import CharmState
from state.haproxy_route import HaproxyRouteRequirersInformation
from state.ingress import IngressRequirersInformation
from state.ingress_per_unit import IngressPerUnitRequirersInformation

APT_PACKAGE_VERSION = "2.8.5-1ubuntu3.3"
APT_PACKAGE_NAME = "haproxy"
HAPROXY_CONFIG_DIR = Path("/etc/haproxy")
HAPROXY_CONFIG = Path(HAPROXY_CONFIG_DIR / "haproxy.cfg")
HAPROXY_USER = "haproxy"
# Configuration used to parameterize Diffie-Hellman key exchange.
# The base64 content of the file is hard-coded here to avoid having to fetch
# the file from https://ssl-config.mozilla.org/ffdhe2048.txt as suggested by Mozilla.
# As the size is 2048, it's safe to use the standard FFDHE parameters.
# They are more compatible, and there aren't concerns about their security.
HAPROXY_DH_PARAM = (
    "-----BEGIN DH PARAMETERS-----\n"
    "MIIBCAKCAQEA//////////+t+FRYortKmq/cViAnPTzx2LnFg84tNpWp4TZBFGQz\n"
    "+8yTnc4kmz75fS/jY2MMddj2gbICrsRhetPfHtXV/WVhJDP1H18GbtCFY2VVPe0a\n"
    "87VXE15/V8k1mE8McODmi3fipona8+/och3xWKE2rec1MKzKT0g6eXq8CrGCsyT7\n"
    "YdEIqUuyyOP7uWrat2DX9GgdT0Kj3jlN9K5W7edjcrsZCwenyO4KbXCeAvzhzffi\n"
    "7MA0BM0oNC9hkXL+nOmFg/+OTxIy7vKBg8P+OxtMb61zO7X8vC7CIAXFjvGDfRaD\n"
    "ssbzSibBsu/6iGtCOGEoXJf//////////wIBAg==\n"
    "-----END DH PARAMETERS-----"
)
HAPROXY_DHCONFIG = Path(HAPROXY_CONFIG_DIR / "ffdhe2048.txt")
HAPROXY_SERVICE = "haproxy"
HAPROXY_INGRESS_CONFIG_TEMPLATE = "haproxy_ingress.cfg.j2"
HAPROXY_INGRESS_PER_UNIT_CONFIG_TEMPLATE = "haproxy_ingress_per_unit.cfg.j2"
HAPROXY_LEGACY_CONFIG_TEMPLATE = "haproxy_legacy.cfg.j2"
HAPROXY_ROUTE_CONFIG_TEMPLATE = "haproxy_route.cfg.j2"
HAPROXY_DEFAULT_CONFIG_TEMPLATE = "haproxy.cfg.j2"
HAPROXY_CERTS_DIR = Path("/var/lib/haproxy/certs")


logger = logging.getLogger()


class HaproxyPackageVersionPinError(Exception):
    """Error when pinning the version of the haproxy package."""


class HaproxyServiceNotActiveError(Exception):
    """Exception raised when both the reverseproxy and ingress relation are established."""


class HaproxyServiceReloadError(Exception):
    """Error when reloading the haproxy service."""


class HaproxyInvalidRelationError(Exception):
    """Exception raised when both the reverseproxy and ingress relation are established."""


class HaproxyValidateConfigError(Exception):
    """Error when validation of the generated haproxy config failed."""


class HAProxyService:
    """HAProxy service class."""

    def install(self) -> None:
        """Install the haproxy apt package."""
        apt.add_package(
            package_names=APT_PACKAGE_NAME, version=APT_PACKAGE_VERSION, update_cache=True
        )
        pin_haproxy_package_version()
        render_file(HAPROXY_DHCONFIG, HAPROXY_DH_PARAM, 0o644)

    def is_active(self) -> bool:
        """Indicate if the haproxy service is active.

        Returns:
            True if the haproxy is running.
        """
        return systemd.service_running(APT_PACKAGE_NAME)

    def reconcile_legacy(self, charm_state: CharmState, services: list) -> None:
        """Render the haproxy config for legacy proxying and reload the service.

        Args:
            charm_state: The charm state component.
            services: List of configuration stanzas for the defined services.
        """
        template_context = {
            "config_global_max_connection": charm_state.global_max_connection,
            "services": services,
        }
        self._render_haproxy_config(HAPROXY_LEGACY_CONFIG_TEMPLATE, template_context)
        self._validate_haproxy_config()
        self._reload_haproxy_service()

    def reconcile_ingress(
        self,
        charm_state: CharmState,
        ingress_requirers_information: (
            IngressRequirersInformation | IngressPerUnitRequirersInformation
        ),
        external_hostname: str,
    ) -> None:
        """Render the haproxy config for ingress proxying and reload the service.

        Args:
            charm_state: The charm's state component.
            ingress_requirers_information: Parsed information about ingress or ingress
                per unit requirers.
            external_hostname: Configured external-hostname for TLS.
        """
        template_context = {
            "config_global_max_connection": charm_state.global_max_connection,
            "ingress_requirers_information": ingress_requirers_information,
            "config_external_hostname": external_hostname,
            "haproxy_crt_dir": HAPROXY_CERTS_DIR,
        }
        template = (
            HAPROXY_INGRESS_CONFIG_TEMPLATE
            if isinstance(ingress_requirers_information, IngressRequirersInformation)
            else HAPROXY_INGRESS_PER_UNIT_CONFIG_TEMPLATE
        )
        self._render_haproxy_config(template, template_context)

        self._validate_haproxy_config()
        self._reload_haproxy_service()

    def reconcile_haproxy_route(
        self,
        charm_state: CharmState,
        haproxy_route_requirers_information: HaproxyRouteRequirersInformation,
    ) -> None:
        """Render the haproxy config for haproxy-route.

        Args:
            charm_state: The charm state component.
            haproxy_route_requirers_information: HaproxyRouteRequirersInformation state component.
        """
        template_context = {
            "config_global_max_connection": charm_state.global_max_connection,
            "backends": haproxy_route_requirers_information.backends,
            "stick_table_entries": haproxy_route_requirers_information.stick_table_entries,
            "peer_units_address": haproxy_route_requirers_information.peers,
            "haproxy_crt_dir": HAPROXY_CERTS_DIR,
        }
        self._render_haproxy_config(HAPROXY_ROUTE_CONFIG_TEMPLATE, template_context)
        self._validate_haproxy_config()
        self._reload_haproxy_service()

    def reconcile_default(self, charm_state: CharmState) -> None:
        """Render the default haproxy config and reload the service.

        Args:
            charm_state (CharmState): The charm state component.
        """
        self._render_haproxy_config(
            HAPROXY_DEFAULT_CONFIG_TEMPLATE,
            {
                "config_global_max_connection": charm_state.global_max_connection,
            },
        )
        self._validate_haproxy_config()
        self._reload_haproxy_service()

    def _render_haproxy_config(self, template_file_path: str, context: dict) -> None:
        """Render the haproxy configuration file.

        Args:
            template_file_path: Path of the template to load.
            context: Context needed to render the template.
        """
        env = Environment(
            loader=FileSystemLoader("templates"),
            autoescape=select_autoescape(),
            keep_trailing_newline=True,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        template = env.get_template(template_file_path)
        rendered = template.render(context)
        render_file(HAPROXY_CONFIG, rendered, 0o644)

    def _reload_haproxy_service(self) -> None:
        """Reload the haproxy service.

        Raises:
            HaproxyServiceReloadError: When the haproxy service fails to reload.
            HaproxyServiceNotActiveError: When the haproxy service is not active after reload.
        """
        try:
            systemd.service_reload(HAPROXY_SERVICE)
        except systemd.SystemdError as exc:
            raise HaproxyServiceReloadError("Failed reloading the haproxy service.") from exc

        if not self.is_active():
            raise HaproxyServiceNotActiveError("HAProxy service is not running.")

    def _validate_haproxy_config(self) -> None:
        """Validate the generated HAProxy config.

        Raises:
            HaproxyValidateConfigError: When validation of the generated HAProxy config failed.
        """
        validate_config_command = ["/usr/sbin/haproxy", "-f", str(HAPROXY_CONFIG), "-c"]
        try:
            # Ignore bandit rule as we're not parsing user input
            subprocess.run(validate_config_command, capture_output=True, check=True)  # nosec B603
        except subprocess.CalledProcessError as exc:
            logger.error(
                "Failed validation the HAProxy config: %s",
                exc.stderr.decode(),
            )
            raise HaproxyValidateConfigError("Failed validating the HAProxy config.") from exc


def render_file(path: Path, content: str, mode: int) -> None:
    """Write a content rendered from a template to a file.

    Args:
        path: Path object to the file.
        content: the data to be written to the file.
        mode: access permission mask applied to the
            file using chmod (e.g. 0o640).
    """
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)
    u = pwd.getpwnam(HAPROXY_USER)
    # Set the correct ownership for the file.
    os.chown(path, uid=u.pw_uid, gid=u.pw_gid)


def read_file(path: Path) -> str:
    """Read the content of a file.

    Args:
        path: Path object to the file.

    Returns:
        str: The content of the file.
    """
    return path.read_text(encoding="utf-8")


def file_exists(path: Path) -> bool:
    """Check if a file exists.

    Args:
        path: Path object to the file.

    Returns:
        bool: True if the file exists.
    """
    return path.exists()


def pin_haproxy_package_version() -> None:
    """Pin the haproxy package version.

    Raises:
        HaproxyPackageVersionPinError: When pinning the haproxy package version failed.
    """
    try:
        # We ignore security warning here as we're not parsing inputs
        run(["/usr/bin/apt-mark", "hold", "haproxy"], check=True)  # nosec
    except CalledProcessError as exc:
        logger.error("Failed calling apt-mark hold haproxy: %s", exc.stderr)
        raise HaproxyPackageVersionPinError("Failed pinning the haproxy package version") from exc
