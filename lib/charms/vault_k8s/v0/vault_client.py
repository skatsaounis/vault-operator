#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
"""Library for interacting with a Vault cluster.

This library shares operations that interact with Vault through its API. It is
intended to be used by charms that need to manage a Vault cluster.
"""

import logging
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Protocol

import hvac
import requests
from hvac.exceptions import Forbidden, InvalidPath, InvalidRequest, VaultError
from requests.exceptions import ConnectionError, RequestException

# The unique Charmhub library identifier, never change it
LIBID = "674754a3268d4507b749ec34214706fd"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 13


RAFT_STATE_ENDPOINT = "v1/sys/storage/raft/autopilot/state"


class LogAdapter(logging.LoggerAdapter):
    """Adapter for the logger to prepend a prefix to all log lines."""

    prefix = "vault_client"

    def process(self, msg, kwargs):
        """Decides the format for the prepended text."""
        return f"[{self.prefix}] {msg}", kwargs


logger = LogAdapter(logging.getLogger(__name__), {})


@dataclass
class Token:
    """Class that represents token authentication for vault.

    This method is the most basic and always available method to access vault.
    """

    token: str

    def login(self, client: hvac.Client):
        """Authenticate a vault client with a token."""
        client.token = self.token


@dataclass
class AppRole:
    """Class that represents approle authentication for vault.

    This method is primarily used to authenticate automation programs for vault.
    """

    role_id: str
    secret_id: str

    def login(self, client: hvac.Client):
        """Authenticate a vault client with approle details."""
        client.auth.approle.login(role_id=self.role_id, secret_id=self.secret_id, use_token=True)


class AuthMethod(Protocol):
    """Classes that implement a login method are auth methods used to log in to Vault."""

    @abstractmethod
    def login(self, client: hvac.Client) -> None:
        """Log in using the given method."""
        raise NotImplementedError


@dataclass
class Certificate:
    """Class that represents a certificate generated by the PKI secrets engine."""

    certificate: str
    ca: str
    chain: List[str]


class AuditDeviceType(Enum):
    """Class that represents the devices that vault supports as device types for audit."""

    FILE = "file"
    SYSLOG = "syslog"
    SOCKET = "socket"


class SecretsBackend(Enum):
    """Class that represents the supported secrets backends by Vault."""

    KV_V2 = "kv-v2"
    PKI = "pki"
    TRANSIT = "transit"


class VaultClientError(Exception):
    """Base class for exceptions raised by the Vault client."""


class Vault:
    """Class to interact with Vault through its API."""

    def __init__(self, url: str, ca_cert_path: Optional[str]):
        self._client = hvac.Client(url=url, verify=ca_cert_path if ca_cert_path else False)

    def authenticate(self, auth_details: AuthMethod) -> bool:
        """Find and use the token related with the given auth method."""
        try:
            auth_details.login(self._client)
        except (VaultError, ConnectionError) as e:
            logger.warning("Failed login to Vault: %s", e)
            return False
        return True

    def get_token_data(self) -> Optional[Dict]:
        """Check if given token is accepted by vault, and returns the token data if so."""
        try:
            token_data = self._client.auth.token.lookup_self()["data"]
        except Forbidden:
            return None
        return token_data

    @property
    def token(self) -> str:
        """Return the token used to authenticate with Vault."""
        return self._client.token

    def is_api_available(self) -> bool:
        """Return whether Vault is available."""
        try:
            self._client.sys.read_health_status(standby_ok=True)
            return True
        except (VaultError, RequestException) as e:
            logger.error("Error while checking Vault health status: %s", e)
            return False

    def is_initialized(self) -> bool:
        """Return whether Vault is initialized."""
        return self._client.sys.is_initialized()

    def is_sealed(self) -> bool:
        """Return whether Vault is sealed."""
        return self._client.sys.is_sealed()

    def needs_migration(self) -> bool:
        """Return true if the vault needs to be migrated, false otherwise."""
        return self._client.seal_status["migration"]  # type: ignore -- bad type hint in stubs

    def get_seal_type(self) -> str:
        """Return the seal type of the Vault."""
        return self._client.seal_status["type"]  # type: ignore -- bad type hint in stubs

    def is_seal_type_transit(self) -> bool:
        """Return whether Vault is sealed by the transit backend."""
        return "transit" == self.get_seal_type()

    def is_active(self) -> bool:
        """Return whether the Vault node is active or not.

        Returns:
            True if initialized, unsealed and active, False otherwise.
        """
        try:
            health_status = self._client.sys.read_health_status()
            return health_status.status_code == 200
        except (VaultError, RequestException) as e:
            logger.error("Error while checking Vault health status: %s", e)
            return False

    def is_active_or_standby(self) -> bool:
        """Return the health status of Vault.

        Returns:
            True if initialized, unsealed and active or standby, False otherwise.
        """
        try:
            health_status = self._client.sys.read_health_status()
            return health_status.status_code == 200 or health_status.status_code == 429
        except (VaultError, RequestException) as e:
            logger.error("Error while checking Vault health status: %s", e)
            return False

    def enable_audit_device(self, device_type: AuditDeviceType, path: str) -> None:
        """Enable a new audit device at the supplied path if it isn't already enabled.

        Args:
            device_type: One of three available device types
            path: The path that will receive audit logs
        """
        try:
            self._client.sys.enable_audit_device(
                device_type=device_type.value,
                options={"file_path": path},
            )
            logger.info("Enabled audit device `%s` for path `%s`", device_type.value, path)
        except InvalidRequest as e:
            if not e.json or not isinstance(e.json, dict):
                raise VaultClientError(e) from e
            errors = e.json.get("errors", [])
            if len(errors) == 1 and errors[0].startswith("path already in use"):
                logger.info("Audit device already enabled.")
            else:
                raise VaultClientError(e) from e
        except VaultError as e:
            raise VaultClientError(e) from e

    def enable_approle_auth_method(self) -> None:
        """Enable approle auth method if it isn't already enabled."""
        try:
            self._client.sys.enable_auth_method("approle")
            logger.info("Enabled approle auth method.")
        except InvalidRequest as e:
            if not e.json or not isinstance(e.json, dict):
                raise VaultClientError(e) from e
            errors = e.json.get("errors", [])
            if len(errors) == 1 and errors[0].startswith("path is already in use"):
                logger.info("Approle already enabled.")
            else:
                raise VaultClientError(e) from e
        except VaultError as e:
            raise VaultClientError(e) from e

    def configure_policy(self, policy_name: str, policy_path: str, **formatting_args: str) -> None:
        """Create/update a policy within vault.

        Args:
            policy_name: Name of the policy to create
            policy_path: The path of the file where the policy is defined, ending with .hcl
            **formatting_args: Additional arguments to format the policy
        """
        with open(policy_path, "r") as f:
            policy = f.read()
        try:
            self._client.sys.create_or_update_policy(
                name=policy_name,
                policy=policy if not formatting_args else policy.format(**formatting_args),
            )
        except VaultError as e:
            raise VaultClientError(e) from e
        logger.debug("Created or updated charm policy: %s", policy_name)

    def configure_approle(
        self,
        role_name: str,
        token_ttl=None,
        token_max_ttl=None,
        policies: Optional[List[str]] = None,
        cidrs: Optional[List[str]] = None,
        token_period=None,
    ) -> str:
        """Create/update a role within vault associating the supplied policies.

        Args:
            role_name: Name of the role to be created or updated
            policies: The attached list of policy names this approle will have access to
            token_ttl: Incremental lifetime for generated tokens, provided as a duration string such as "5m"
            token_max_ttl: Maximum lifetime for generated tokens, provided as a duration string such as "5m"
            token_period: The period within which the token must be renewed. See Vault documentation for more information.
            cidrs: The list of IP networks that are allowed to authenticate
        """
        self._client.auth.approle.create_or_update_approle(
            role_name,
            bind_secret_id="true",
            token_ttl=token_ttl,
            token_max_ttl=token_max_ttl,
            token_policies=policies,
            token_bound_cidrs=cidrs,
            token_period=token_period,
        )
        response = self._client.auth.approle.read_role_id(role_name)
        return response["data"]["role_id"]

    def generate_role_secret_id(self, name: str, cidrs: Optional[List[str]] = None) -> str:
        """Generate a new secret tied to an AppRole."""
        response = self._client.auth.approle.generate_secret_id(name, cidr_list=cidrs)
        return response["data"]["secret_id"]

    def read_role_secret(self, name: str, id: str) -> dict:
        """Get definition of a secret tied to an AppRole."""
        response = self._client.auth.approle.read_secret_id(name, id)
        return response["data"]

    def enable_secrets_engine(self, backend_type: SecretsBackend, path: str) -> None:
        """Enable given secret engine on the given path."""
        try:
            self._client.sys.enable_secrets_engine(
                backend_type=backend_type.value,
                description=f"Charm created '{backend_type.value}' backend",
                path=path,
            )
            logger.info("Enabled %s backend", backend_type.value)
        except InvalidRequest as e:
            # TODO: Fix the type stubs for hvac to properly identify the json attribute
            if not e.json or not isinstance(e.json, dict):
                raise VaultClientError(e) from e
            errors = e.json.get("errors", [])
            if len(errors) == 1 and errors[0].startswith("path is already in use"):
                logger.info("%s backend already enabled", backend_type.value)
            else:
                raise VaultClientError(e) from e

    def disable_secrets_engine(self, path: str) -> None:
        """Disable the secret engine at the given path."""
        try:
            self._client.sys.disable_secrets_engine(path)
            logger.info("Disabled secret engine at %s", path)
        except InvalidPath:
            logger.info("Secret engine at `%s` is already disabled", path)

    def is_secret_engine_enabled(self, path: str) -> bool:
        """Check if a mount is enabled."""
        return f"{path}/" in self._client.sys.list_mounted_secrets_engines()

    def is_intermediate_ca_set(self, mount: str, certificate: str) -> bool:
        """Check if the intermediate CA is set for the PKI backend."""
        intermediate_ca = self._client.secrets.pki.read_ca_certificate(mount_point=mount)
        return intermediate_ca == certificate

    def get_intermediate_ca(self, mount: str) -> str:
        """Get the intermediate CA for the PKI backend."""
        return self._client.secrets.pki.read_ca_certificate(mount_point=mount)

    def generate_pki_intermediate_ca_csr(self, mount: str, common_name: str) -> str:
        """Generate an intermediate CA CSR for the PKI backend.

        Returns:
            str: The Certificate Signing Request.
        """
        response = self._client.secrets.pki.generate_intermediate(
            mount_point=mount,
            common_name=common_name,
            type="internal",
        )
        logger.info("Generated a CSR for the intermediate CA for the PKI backend")
        return response["data"]["csr"]

    def set_pki_intermediate_ca_certificate(self, certificate: str, mount: str) -> None:
        """Set the intermediate CA certificate for the PKI backend."""
        self._client.secrets.pki.set_signed_intermediate(
            certificate=certificate, mount_point=mount
        )
        logger.info("Set the intermediate CA certificate for the PKI backend")

    def sign_pki_certificate_signing_request(
        self,
        mount: str,
        role: str,
        csr: str,
        common_name: str,
    ) -> Optional[Certificate]:
        """Sign a certificate signing request for the PKI backend.

        Args:
            mount: The PKI mount point.
            role: The role to use for signing the certificate.
            csr: The certificate signing request.
            common_name: The common name for the certificate.

        Returns:
            Certificate: The signed certificate object
        """
        try:
            response = self._client.secrets.pki.sign_certificate(
                csr=csr,
                mount_point=mount,
                common_name=common_name,
                name=role,
            )
            logger.info("Signed a PKI certificate for %s", common_name)
            return Certificate(
                certificate=response["data"]["certificate"],
                ca=response["data"]["issuing_ca"],
                chain=response["data"]["ca_chain"],
            )
        except InvalidRequest as e:
            logger.warning("Error while signing PKI certificate: %s", e)
            return None

    def create_or_update_pki_charm_role(self, role: str, allowed_domains: str, mount: str) -> None:
        """Create a role for the PKI backend."""
        self._client.secrets.pki.create_or_update_role(
            name=role,
            mount_point=mount,
            extra_params={
                "allowed_domains": allowed_domains,
                "allow_subdomains": True,
            },
        )
        logger.info("Created a role for the PKI backend")

    def is_pki_role_created(self, role: str, mount: str) -> bool:
        """Check if the role is created for the PKI backend."""
        try:
            existing_roles = self._client.secrets.pki.list_roles(mount_point=mount)
            return role in existing_roles["data"]["keys"]
        except InvalidPath:
            return False

    def create_snapshot(self) -> requests.Response:
        """Create a snapshot of the Vault data."""
        return self._client.sys.take_raft_snapshot()

    def restore_snapshot(self, snapshot: bytes) -> requests.Response:
        """Restore a snapshot of the Vault data.

        Uses force_restore_raft_snapshot to restore the snapshot
        even if the unseal key used at backup time is different from the current one.
        """
        return self._client.sys.force_restore_raft_snapshot(snapshot)

    def get_raft_cluster_state(self) -> dict:
        """Get raft cluster state."""
        response = self._client.adapter.get(RAFT_STATE_ENDPOINT)
        return response["data"]

    def is_raft_cluster_healthy(self) -> bool:
        """Check if raft cluster is healthy."""
        return self.get_raft_cluster_state()["healthy"]

    def remove_raft_node(self, node_id: str) -> None:
        """Remove raft peer."""
        self._client.sys.remove_raft_node(server_id=node_id)
        logger.info("Removed raft node %s", node_id)

    def is_node_in_raft_peers(self, node_id: str) -> bool:
        """Check if node is in raft peers."""
        raft_config = self._client.sys.read_raft_config()
        for peer in raft_config["data"]["config"]["servers"]:
            if peer["node_id"] == node_id:
                return True
        return False

    def get_num_raft_peers(self) -> int:
        """Return the number of raft peers."""
        raft_config = self._client.sys.read_raft_config()
        return len(raft_config["data"]["config"]["servers"])

    def is_common_name_allowed_in_pki_role(self, role: str, mount: str, common_name: str) -> bool:
        """Return whether the provided common name is in the list of domains allowed by the specified PKI role."""
        try:
            return common_name in self._client.secrets.pki.read_role(
                name=role, mount_point=mount
            ).get("data", {}).get("allowed_domains", [])
        except InvalidPath:
            logger.error("Role does not exist on the specified path.")
            return False

    def make_latest_pki_issuer_default(self, mount: str) -> None:
        """Update the issuers config to always make the latest issuer created default issuer."""
        try:
            first_issuer = self._client.secrets.pki.list_issuers(mount_point=mount)["data"][
                "keys"
            ][0]
        except (InvalidPath, KeyError) as e:
            logger.error("No issuers found on the specified path: %s", e)
            raise VaultClientError("No issuers found on the specified path.")
        try:
            issuers_config = self._client.read(path=f"{mount}/config/issuers")
            if issuers_config and not issuers_config["data"]["default_follows_latest_issuer"]:  # type: ignore -- bad type hint in stubs
                logger.debug("Updating issuers config")
                self._client.write_data(
                    path=f"{mount}/config/issuers",
                    data={
                        "default_follows_latest_issuer": True,
                        "default": first_issuer,
                    },
                )
        except (TypeError, KeyError):
            logger.error("Issuers config is not yet created")

    def _get_autounseal_policy_name(self, relation_id: int) -> str:
        """Return the policy name for the given relation id."""
        return f"charm-autounseal-{relation_id}"

    def _get_autounseal_approle_name(self, relation_id: int) -> str:
        """Return the approle name for the given relation id."""
        return f"charm-autounseal-{relation_id}"

    def _get_autounseal_key_name(self, relation_id: int) -> str:
        """Return the key name for the given relation id."""
        return str(relation_id)

    def _create_autounseal_key(self, mount_point: str, relation_id: int) -> str:
        """Create a new autounseal key."""
        key_name = self._get_autounseal_key_name(relation_id)
        response = self._client.secrets.transit.create_key(mount_point=mount_point, name=key_name)
        logging.debug(f"Created a new autounseal key: {response}")
        return key_name

    def _destroy_autounseal_key(self, mount_point, key_name):
        """Destroy the autounseal key."""
        self._client.secrets.transit.delete_key(mount_point=mount_point, name=key_name)

    def destroy_autounseal_credentials(self, relation_id: int, mount: str) -> None:
        """Destroy the approle and transit key for the given relation id."""
        # Remove the approle
        role_name = self._get_autounseal_approle_name(relation_id)
        self._client.auth.approle.delete_role(role_name)
        # Remove the policy
        policy_name = self._get_autounseal_policy_name(relation_id)
        self._client.sys.delete_policy(policy_name)
        # Remove the transit key
        # FIXME: This is currently disabled because we haven't figured out how
        # to properly handle destroying the relation, yet. Destroying the key
        # without migrating would make it impossible to recover the vault.
        # key_name = self.get_autounseal_key_name(relation_id)
        # self._destroy_autounseal_key(mount, key_name)

    def create_autounseal_credentials(
        self, relation_id: int, mount: str, policy_path: str
    ) -> tuple[str, str, str]:
        """Create auto-unseal credentials for the given relation id.

        Args:
            relation_id: The Juju relation id to use for the approle.
            mount: The mount point for the transit backend.
            policy_path: Path to a file that contains the autounseal policy.

        Returns:
            A tuple containing the Role Id, Secret Id and Key Name.

        """
        key_name = self._create_autounseal_key(mount, relation_id)
        policy_name = self._get_autounseal_policy_name(relation_id)
        self.configure_policy(policy_name, policy_path, mount=mount, key_name=key_name)

        role_name = self._get_autounseal_approle_name(relation_id)
        role_id = self.configure_approle(role_name, policies=[policy_name], token_period="60s")
        secret_id = self.generate_role_secret_id(role_name)
        return key_name, role_id, secret_id
