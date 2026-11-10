"""Email notifications. Only sent when a run has warnings or errors."""

import smtplib
from email.message import EmailMessage


def send_problem_email(settings, run_id, problems):
    lines = [f"Run #{run_id} of the Dawarich track fixer reported {len(problems)} problem(s):", ""]
    lines += [f"- {level.upper()}: {message}" for level, message in problems]
    if settings.app_url:
        lines += ["", f"Details: {settings.app_url}/runs/{run_id}"]
    send_email(settings, f"Dawarich track fixer: {len(problems)} problem(s) in run #{run_id}", "\n".join(lines))


def send_test_email(settings):
    send_email(settings, "Dawarich track fixer: test email", "Email notifications work.")


def send_email(settings, subject, text):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.email_from
    message["To"] = settings.email_to
    message.set_content(text)

    if settings.smtp_security == "ssl":
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with connection:
        if settings.smtp_security == "starttls":
            connection.starttls()
        if settings.smtp_username:
            connection.login(settings.smtp_username, settings.smtp_password)
        connection.send_message(message)
