from core.database import get_db


def save_message(user_id, username, role, content):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO messages (user_id, username, role, content)
        VALUES (?, ?, ?, ?)
        """,
        (str(user_id), username, role, content),
    )
    conn.commit()
    conn.close()


def get_recent_messages(user_id, limit=10):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE user_id=?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(user_id), limit),
    ).fetchall()
    conn.close()
    rows.reverse()
    return rows
