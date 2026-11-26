"""Sign-ins under way and the sessions ROHub started, in the database (oidc-identity, sections 5, 8 and 11).

Plain functions on a connection, so that a caller decides what belongs to one transaction. Values
that could open something, ``state``, the browser's key and session tokens, reach this module as
hashes only.
"""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from typing import Optional

from ..account_model import ACCOUNT_COLUMNS

MAX_TRANSACTIONS = 10_000
"""Most sign-ins under way that are kept; the oldest go first."""


@dataclass(frozen=True)
class Transaction:
    """One sign-in on its way to the provider and back.

    Attributes:
        nonce: What the ID token must carry.
        code_verifier: The PKCE verifier.
        issuer: The issuer it began with.
        redirect: The validated page to return to.
        created_at: When it began (ISO-8601 UTC).
        expires_at: When it stops being usable (ISO-8601 UTC).
    """

    nonce: str
    code_verifier: str
    issuer: str
    redirect: str
    created_at: str
    expires_at: str


def add_transaction(
    conn: sqlite3.Connection, transaction: Transaction, *, state_hash: str, browser_key_hash: str
) -> None:
    """Store a sign-in, delete the expired ones and the oldest beyond the bound.

    Args:
        conn: An immediate transaction's connection.
        transaction: The sign-in.
        state_hash: The hash of its ``state``.
        browser_key_hash: The hash of the key its browser holds.
    """
    conn.execute("DELETE FROM oidc_transactions WHERE expires_at <= ?", (transaction.created_at,))
    conn.execute(
        """
        INSERT INTO oidc_transactions (
            state_hash, browser_key_hash, nonce, code_verifier, issuer, redirect, created_at, expires_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state_hash,
            browser_key_hash,
            transaction.nonce,
            transaction.code_verifier,
            transaction.issuer,
            transaction.redirect,
            transaction.created_at,
            transaction.expires_at,
        ),
    )
    conn.execute(
        """
        DELETE FROM oidc_transactions WHERE rowid IN (
            SELECT rowid FROM oidc_transactions ORDER BY rowid
             LIMIT MAX(0, (SELECT COUNT(*) FROM oidc_transactions) - ?)
        )
        """,
        (MAX_TRANSACTIONS,),
    )


def take_transaction(conn: sqlite3.Connection, *, state_hash: str, browser_key_hash: str) -> Optional[Transaction]:
    """Return the sign-in a browser names and delete it, so that it serves once.

    A request from another browser gets nothing and deletes nothing. The caller judges whether the
    sign-in has expired or began with another issuer.

    Args:
        conn: An immediate transaction's connection.
        state_hash: The hash of the returned ``state``.
        browser_key_hash: The hash of the key the browser sent.
    """
    row = conn.execute("SELECT * FROM oidc_transactions WHERE state_hash = ?", (state_hash,)).fetchone()
    if row is None or not hmac.compare_digest(row["browser_key_hash"], browser_key_hash):
        return None
    conn.execute("DELETE FROM oidc_transactions WHERE state_hash = ?", (state_hash,))
    return Transaction(
        nonce=row["nonce"],
        code_verifier=row["code_verifier"],
        issuer=row["issuer"],
        redirect=row["redirect"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def add_session(
    conn: sqlite3.Connection,
    *,
    token_hash: str,
    user_id: int,
    issuer: str,
    subject: str,
    sid: Optional[str],
    created_at: str,
    expires_at: str,
    ip_address: str,
    user_agent: str,
) -> bool:
    """Store a session of an account that is not blocked, and delete the expired ones.

    Args:
        conn: The transaction's connection.
        token_hash: The hash of its token.
        user_id: The account.
        issuer: The provider's issuer.
        subject: The subject the provider gives the person.
        sid: The provider's session id, when it gave one.
        created_at: When it starts.
        expires_at: When it ends, however much it is used.
        ip_address: The client's address.
        user_agent: The client's browser.

    Returns:
        Whether it was stored; a blocked account gets none.
    """
    conn.execute("DELETE FROM oidc_sessions WHERE expires_at <= ?", (created_at,))
    cursor = conn.execute(
        """
        INSERT INTO oidc_sessions (
            token_hash, user_id, issuer, subject, sid, created_at, expires_at, ip_address, user_agent
        )
        SELECT ?, id, ?, ?, ?, ?, ?, ?, ? FROM users WHERE id = ? AND is_active = 1
        """,
        (token_hash, issuer, subject, sid, created_at, expires_at, ip_address, user_agent, user_id),
    )
    return bool(cursor.rowcount)


def session_account(conn: sqlite3.Connection, *, token_hash: str, issuer: str, now: str) -> Optional[sqlite3.Row]:
    """Return the account row of a session that counts, or ``None``.

    Args:
        conn: An open connection.
        token_hash: The hash of the session's token.
        issuer: The configured issuer; a session of another one does not count.
        now: The current time (ISO-8601 UTC).
    """
    row: Optional[sqlite3.Row] = conn.execute(
        f"""
        SELECT {ACCOUNT_COLUMNS}
          FROM oidc_sessions
          JOIN users ON users.id = oidc_sessions.user_id
         WHERE oidc_sessions.token_hash = ?
           AND oidc_sessions.issuer = ?
           AND oidc_sessions.expires_at > ?
           AND users.is_active = 1
        """,
        (token_hash, issuer, now),
    ).fetchone()
    return row


def delete_ended_session(conn: sqlite3.Connection, *, token_hash: str, now: str) -> None:
    """Delete a session that has expired or whose account is blocked; one of another issuer stays.

    Args:
        conn: The transaction's connection.
        token_hash: The hash of the session's token.
        now: The current time (ISO-8601 UTC).
    """
    conn.execute(
        """
        DELETE FROM oidc_sessions
         WHERE token_hash = ?
           AND NOT EXISTS (
               SELECT 1 FROM users
                WHERE users.id = oidc_sessions.user_id AND users.is_active = 1 AND oidc_sessions.expires_at > ?
           )
        """,
        (token_hash, now),
    )


def delete_session(conn: sqlite3.Connection, token_hash: str) -> None:
    """Delete one session.

    Args:
        conn: The transaction's connection.
        token_hash: The hash of its token.
    """
    conn.execute("DELETE FROM oidc_sessions WHERE token_hash = ?", (token_hash,))


def end_sessions(conn: sqlite3.Connection, *, issuer: str, sid: Optional[str], subject: Optional[str]) -> int:
    """End the sessions a logout token names: those with its ``sid`` and, when given, its ``sub``; without a ``sid`` all of the ``sub``.

    Args:
        conn: The transaction's connection.
        issuer: The provider's issuer.
        sid: The provider's session id, or ``None``.
        subject: The subject, or ``None``; one of the two is given.

    Returns:
        How many ended.
    """
    conditions, values = ["issuer = ?"], [issuer]
    for column, value in (("sid", sid), ("subject", subject)):
        if value is not None:
            conditions.append(f"{column} = ?")
            values.append(value)
    if len(values) == 1:
        return 0
    return conn.execute(f"DELETE FROM oidc_sessions WHERE {' AND '.join(conditions)}", values).rowcount
