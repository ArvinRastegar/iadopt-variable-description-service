"""Operator commands, run in the backend container with the server's settings.

``python -m app.cli db status|migrate|backup|reclaim`` (``components/app-storage.md``, section 7),
``owner bind|rebind`` and ``legacy show|link|fold|purge-passwords``
(``components/accounts-roles.md``, sections 5 and 8), ``oidc check``
(``components/keycloak-deployment.md``, section 5). Each is safe beside a running backend. Exit
codes: 0 on success, 1 on a refusal or failure (with the error's name and message), 2 on a usage
error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any, Callable, Dict, Optional, Sequence
from urllib.parse import urlsplit

from . import storage
from .accounts import CommandRefused
from .core.dependencies import account_store, auth_store
from .core.logging import configure_logging
from .oidc import check as oidc_check

Command = Callable[[argparse.Namespace], Optional[int]]


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one command and return its exit code.

    Args:
        argv: The arguments after ``python -m app.cli``; ``None`` for ``sys.argv[1:]``.
    """
    configure_logging()
    try:
        arguments = _parser().parse_args(argv)
        if "check" in arguments:
            arguments.check(arguments)
    except SystemExit as e:  # argparse printed a usage error (2) or the help (0)
        return int(e.code or 0)
    try:
        code: Optional[int] = arguments.command(arguments)
    except (CommandRefused, storage.StorageError, sqlite3.Error, OSError) as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return code or 0


# ------------------------------------------------------------------------------------------------
# db
# ------------------------------------------------------------------------------------------------


def _db_status(_: argparse.Namespace) -> None:
    """Print the database's sizes, space, migrations and tables."""
    _print_json(storage.status())


def _db_migrate(_: argparse.Namespace) -> None:
    """Apply the pending migrations and print their ids."""
    applied = storage.migrate()
    print("\n".join(f"Applied {id_}" for id_ in applied) or "Nothing is pending.")


def _db_backup(_: argparse.Namespace) -> None:
    """Make a verified backup and print its path and size."""
    path = storage.backup("manual")
    print(f"{path} ({path.stat().st_size} bytes)")


def _db_reclaim(_: argparse.Namespace) -> None:
    """Return free pages to the file system and print the sizes before and after."""
    _print_json(storage.reclaim_space())


# ------------------------------------------------------------------------------------------------
# owner
# ------------------------------------------------------------------------------------------------


def _owner_bind(arguments: argparse.Namespace) -> None:
    """Bind the owner to an existing account."""
    account = account_store.bind_owner(**_owner_target(arguments))
    _audit("owner.bound", account_id=account["id"])
    print(f"The owner is {account['username']} (account {account['id']}).")


def _owner_rebind(arguments: argparse.Namespace) -> None:
    """Move the owner binding to another account."""
    account = account_store.rebind_owner(reason=arguments.reason, **_owner_target(arguments))
    _audit("owner.rebound", account_id=account["id"], reason=arguments.reason)
    print(f"The owner is now {account['username']} (account {account['id']}).")


def _owner_target(arguments: argparse.Namespace) -> Dict[str, Any]:
    """The account an owner command names, as the account store takes it."""
    identity = (arguments.issuer, arguments.subject) if arguments.issuer else None
    return {"username": arguments.username, "account_id": arguments.account, "identity": identity}


# ------------------------------------------------------------------------------------------------
# legacy
# ------------------------------------------------------------------------------------------------


def _legacy_show(arguments: argparse.Namespace) -> None:
    """Print what an account is and holds."""
    _print_json(account_store.describe(username=arguments.username, account_id=arguments.account))


def _legacy_link(arguments: argparse.Namespace) -> None:
    """Pre-link an identity-provider subject to a legacy local account."""
    account = account_store.link_identity(
        account_id=arguments.account,
        issuer=arguments.issuer,
        subject=arguments.subject,
        reason=arguments.reason,
        keep_admin=arguments.keep_admin,
    )
    _audit("legacy.identity_linked", account_id=account["id"], reason=arguments.reason)
    print(f"{arguments.subject} now leads to {account['username']} (account {account['id']}, {account['role']}).")


def _legacy_fold(arguments: argparse.Namespace) -> None:
    """Move a new identity-provider account's identities into a legacy local account."""
    account = account_store.fold_account(
        from_id=arguments.from_, into_id=arguments.into, reason=arguments.reason, keep_admin=arguments.keep_admin
    )
    _audit("legacy.account_folded", account_id=account["id"], from_account_id=arguments.from_, reason=arguments.reason)
    print(
        f"Account {arguments.from_} is folded into {account['username']} (account {account['id']}, {account['role']}) "
        "and blocked."
    )


def _legacy_purge_passwords(arguments: argparse.Namespace) -> None:
    """Replace every usable password hash, or say how many there are."""
    if not arguments.confirm_cutover:
        print(
            f"{account_store.count_usable_passwords()} accounts have a usable password. Nothing was changed; "
            "pass --confirm-cutover once the owner confirmed the cutover."
        )
        return
    purged = account_store.purge_passwords()
    for account in purged:
        _audit("legacy.password_purged", account_id=account["id"])
    print(f"Purged the passwords of {len(purged)} accounts. No password login succeeds any more.")


# ------------------------------------------------------------------------------------------------
# oidc
# ------------------------------------------------------------------------------------------------


def _oidc_check(arguments: argparse.Namespace) -> int:
    """Check the configured identity provider; it gets requests, nothing is written."""
    return 0 if oidc_check.run(pkce=arguments.pkce, refused_callbacks=arguments.refused_callback) else 1


# ------------------------------------------------------------------------------------------------
# Parsing and printing
# ------------------------------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """The command line of all groups."""
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="I-ADOPT backend operator commands.")
    groups = parser.add_subparsers(title="groups", required=True, metavar="GROUP")

    database = _group(groups, "db", "The database: status, migrations, backups and space.")
    _command(database, "status", _db_status, "Print sizes, free space, migrations and tables; writes nothing.")
    _command(database, "migrate", _db_migrate, "Apply the pending migrations, after a verified backup.")
    _command(database, "backup", _db_backup, "Make a verified manual backup beside the database.")
    _command(database, "reclaim", _db_reclaim, "Return free pages to the file system; deletes no row.")

    owner = _group(groups, "owner", "The owner, who grants and revokes admin.")
    bind = _command(owner, "bind", _owner_bind, "Bind the owner to an existing account, once.")
    _add_owner_target(bind)
    rebind = _command(owner, "rebind", _owner_rebind, "Move the binding, or restore the owner's account.")
    _add_owner_target(rebind)
    _add_reason(rebind)

    legacy = _group(groups, "legacy", "Local password accounts at the cutover to the identity provider.")
    show = _command(legacy, "show", _legacy_show, "Print an account's role, identities, data and history.")
    target = show.add_mutually_exclusive_group(required=True)
    target.add_argument("--account", type=int, metavar="ID")
    target.add_argument("--username", metavar="NAME")
    link = _command(legacy, "link", _legacy_link, "Let an identity-provider subject sign in to a local account.")
    link.add_argument("--account", type=int, required=True, metavar="ID")
    link.add_argument("--issuer", required=True, type=_text, metavar="ISS")
    link.add_argument("--subject", required=True, type=_text, metavar="SUB")
    _add_reason(link)
    _add_keep_admin(link)
    fold = _command(legacy, "fold", _legacy_fold, "Move a new, empty account's identities into a local account.")
    fold.add_argument("--from", dest="from_", type=int, required=True, metavar="ID")
    fold.add_argument("--into", type=int, required=True, metavar="ID")
    _add_reason(fold)
    _add_keep_admin(fold)
    purge = _command(legacy, "purge-passwords", _legacy_purge_passwords, "End password logins for good.")
    purge.add_argument("--confirm-cutover", action="store_true", help="without it nothing is changed")

    oidc = _group(groups, "oidc", "The identity provider people sign in with (ROHub).")
    check = _command(oidc, "check", _oidc_check, "Check the configured provider; sends requests to it, writes nothing.")
    check.add_argument("--pkce", action="store_true", help="also see that the client requires PKCE S256")
    check.add_argument(
        "--refused-callback",
        action="append",
        default=[],
        type=_address,
        metavar="URL",
        help="also see that this callback is refused; may be given several times",
    )
    return parser


def _group(groups: Any, name: str, text: str) -> Any:
    """Add a command group and return what its commands are added to."""
    group = groups.add_parser(name, help=text, description=text)
    return group.add_subparsers(title="commands", required=True, metavar="COMMAND")


def _command(group: Any, name: str, command: Command, text: str) -> argparse.ArgumentParser:
    """Add a command to a group."""
    parser: argparse.ArgumentParser = group.add_parser(name, help=text, description=text)
    parser.set_defaults(command=command)
    return parser


def _add_owner_target(parser: argparse.ArgumentParser) -> None:
    """Add the options that name one account: a username, an id, or an identity it holds."""
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--username", metavar="NAME")
    target.add_argument("--account", type=int, metavar="ID")
    target.add_argument("--issuer", type=_text, metavar="ISS", help="with --subject: an identity the account holds")
    parser.add_argument("--subject", type=_text, metavar="SUB")
    parser.set_defaults(check=lambda arguments: _check_identity_options(parser, arguments))


def _check_identity_options(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> None:
    """Refuse an issuer without a subject, or a subject without an issuer, as a usage error."""
    if bool(arguments.issuer) != bool(arguments.subject):
        parser.error("--issuer and --subject go together")


def _add_reason(parser: argparse.ArgumentParser) -> None:
    """Add the required reason of a change."""
    parser.add_argument("--reason", required=True, type=_text, metavar="TEXT")


def _add_keep_admin(parser: argparse.ArgumentParser) -> None:
    """Add the option that lets an admin stay one."""
    parser.add_argument("--keep-admin", action="store_true", help="an admin stays an admin")


def _text(value: str) -> str:
    """Trim a text argument and refuse an empty one.

    Raises:
        argparse.ArgumentTypeError: If nothing is left.
    """
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("must not be empty")
    return text


def _address(value: str) -> str:
    """Return an absolute ``http`` or ``https`` address, trimmed; anything else is a mistake, not a refused callback.

    Raises:
        argparse.ArgumentTypeError: If it is not one.
    """
    text = _text(value)
    try:
        parts = urlsplit(text)
        ok = parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
    except ValueError:
        ok = False
    if not ok:
        raise argparse.ArgumentTypeError(f"not an absolute http or https address: {text}")
    return text


def _print_json(value: Any) -> None:
    """Print a report as indented JSON."""
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _audit(action: str, **metadata: Any) -> None:
    """Write the audit event of an offline change; its actor is whoever runs the command."""
    auth_store.audit_event(action=action, metadata={"actor": "offline", **metadata})


if __name__ == "__main__":
    sys.exit(main())
