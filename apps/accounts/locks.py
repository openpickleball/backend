"""Account row locks shared by the paths that race Account deletion (ADR 0048)."""

from django.contrib.auth import get_user_model
from django.db import connection


def hold_active_account(account_id: int) -> bool:
    """Lock the Account row ``FOR KEY SHARE``; True when it is active.

    Call inside the write transaction, before any other row lock. Account
    deletion takes the row ``FOR UPDATE`` first, so a caller that waited
    on it reads the committed, inactive row and refuses, and a deletion
    that comes later waits for the caller's commit and then sees its
    rows. ``FOR KEY SHARE`` conflicts only with ``FOR UPDATE`` (deletion,
    ``create_player``). Django has no share modes, so this is raw SQL.
    """
    table = connection.ops.quote_name(get_user_model()._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT is_active FROM {table} WHERE id = %s FOR KEY SHARE",
            [account_id],
        )
        row = cursor.fetchone()
    return bool(row and row[0])
