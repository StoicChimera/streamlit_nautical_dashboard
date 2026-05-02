import os
import sys
import secrets
import string
import bcrypt
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    sys.exit("Missing SUPABASE_CONN environment variable.")

engine = create_engine(SUPABASE_CONN)


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)):
            return pw


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def set_password(email: str, password: str) -> None:
    pw_hash = hash_password(password)
    with engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE dim_app_users
            SET pw_hash             = :pw_hash,
                must_change_pw      = TRUE,
                failed_login_count  = 0,
                lockout_until       = NULL
            WHERE LOWER(email) = LOWER(:email)
            RETURNING email, name
        """), {"pw_hash": pw_hash, "email": email})
        row = result.fetchone()
    if row:
        print(f"Password set for {row.name} ({row.email})")
        print(f"Initial password: {password}")
        print("User must change password on first login.")
    else:
        sys.exit(f"No user found with email {email}")


def list_users() -> None:
    import pandas as pd
    df = pd.read_sql(text("""
        SELECT email, name, role, active,
               pw_hash IS NOT NULL AS has_password,
               must_change_pw, last_login_at, failed_login_count
        FROM dim_app_users ORDER BY name
    """), engine)
    print(df.to_string(index=False))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "--list":
        list_users()
    elif len(sys.argv) >= 3 and sys.argv[2] == "--random":
        email = sys.argv[1]
        pw = generate_password()
        set_password(email, pw)
    elif len(sys.argv) >= 3:
        email, password = sys.argv[1], sys.argv[2]
        if len(password) < 12:
            sys.exit("Password must be at least 12 characters.")
        set_password(email, password)
    else:
        print(__doc__)
        sys.exit(1)