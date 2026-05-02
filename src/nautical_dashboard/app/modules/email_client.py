"""
email_client.py
===============

Thin wrapper around Resend API for transactional email.

Configuration:
    RESEND_API_KEY        — required, from Resend dashboard
    APP_URL               — required for password reset emails (e.g.,
                            https://stoicchimera-streamlit-nautica-...streamlit.app)
    EMAIL_FROM            — defaults to noreply@praetorsol.com
"""

import os
import logging
import resend
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.prod")  # support both naming conventions

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
APP_URL        = os.getenv("APP_URL", "").rstrip("/")
EMAIL_FROM     = os.getenv("EMAIL_FROM", "Nautical Financial Platform <noreply@praetorsol.com>")

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


def _is_configured() -> bool:
    return bool(RESEND_API_KEY) and bool(APP_URL)


def send_password_reset_email(to_email: str, token: str, user_name: str) -> bool:
    """
    Sends a password reset email with a one-hour link.
    Returns True on success, False on failure (logged, not raised).
    """
    if not _is_configured():
        logging.error(
            "Email not configured. RESEND_API_KEY=%s APP_URL=%s",
            bool(RESEND_API_KEY), bool(APP_URL),
        )
        return False

    reset_url = f"{APP_URL}/?reset_token={token}"

    html_body = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 560px; margin: 0 auto; padding: 24px; color: #1f2937;">
        <h2 style="color: #111827; margin-bottom: 4px;">Password reset</h2>
        <p style="color: #6b7280; margin-top: 0;">Nautical Financial Platform</p>

        <p>Hi {user_name},</p>

        <p>Someone (hopefully you) requested a password reset for your account.
        Click the button below to set a new password. This link expires in <strong>1 hour</strong>.</p>

        <p style="margin: 32px 0;">
            <a href="{reset_url}"
               style="background: #2563eb; color: white; padding: 12px 24px;
                      text-decoration: none; border-radius: 6px; font-weight: 600;
                      display: inline-block;">
                Reset password
            </a>
        </p>

        <p style="color: #6b7280; font-size: 14px;">
            Or copy this link into your browser:<br>
            <span style="word-break: break-all;">{reset_url}</span>
        </p>

        <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 32px 0;">

        <p style="color: #9ca3af; font-size: 13px;">
            If you didn't request this, ignore this email. Your password won't change
            unless you click the link and set a new one. The link expires automatically
            in 1 hour.
        </p>
    </body>
    </html>
    """

    text_body = f"""
Password reset — Nautical Financial Platform

Hi {user_name},

Someone (hopefully you) requested a password reset for your account.
Click the link below to set a new password. This link expires in 1 hour.

{reset_url}

If you didn't request this, ignore this email. Your password won't change
unless you click the link and set a new one.
    """.strip()

    try:
        resend.Emails.send({
            "from":    EMAIL_FROM,
            "to":      [to_email],
            "subject": "Password reset — Nautical Financial Platform",
            "html":    html_body,
            "text":    text_body,
        })
        return True
    except Exception as e:
        logging.exception("Failed to send password reset email to %s: %s", to_email, e)
        return False