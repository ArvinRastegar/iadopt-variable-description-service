"""Accounts, identities, the owner and admin roles and blocks (``components/accounts-roles.md``).

Contract approved under gate G-017. This is the skeleton of the increment-1 tests gate: the
account side of the identity-provider login that ``oidc-identity`` calls from increment 2. Like
``RecordStore``, the store follows the auth store's database path on every call. Its methods raise
``NotImplementedError`` until the implementation gate.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from .auth import AuthStore

_TODO = "accounts-roles is specified (components/accounts-roles.md) but not implemented yet."


class AccountStore:
    """Identity-provider accounts kept in the auth database, keyed by (issuer, subject)."""

    def __init__(self, auth: AuthStore) -> None:
        """Bind the store to the auth store whose database and accounts it shares.

        Args:
            auth: The process-wide auth store.
        """
        self.auth = auth

    def account_for_identity(self, issuer: str, subject: str) -> Optional[Dict[str, Any]]:
        """Return the account holding the identity, or ``None``; never create one (section 7).

        Args:
            issuer: The identity provider's issuer URL.
            subject: The subject the provider gives the person.

        Raises:
            NotImplementedError: Until the implementation gate.
        """
        raise NotImplementedError(_TODO)

    def ensure_account(self, issuer: str, subject: str, claims: Mapping[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Return the account holding the identity, creating an ordinary one when there is none.

        Section 7: refreshes email, ``email_verified``, display name and last login of a found
        account without touching its role or block state; never matches by email, username or
        name, and never reads roles or groups from the claims. The account is the auth store's
        user dict with ``email_verified`` added.

        Args:
            issuer: An absolute ``http`` or ``https`` URL, never ``local``.
            subject: 1 to 255 characters.
            claims: The identity provider's claims.

        Raises:
            NotImplementedError: Until the implementation gate.
        """
        raise NotImplementedError(_TODO)
