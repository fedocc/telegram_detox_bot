from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from app.config import Settings


class EmailSendError(RuntimeError):
    pass


class EmailSender:
    def __init__(self, settings: Settings):
        self.settings = settings

    def send(
        self,
        subject: str,
        text: str,
        html: str | None = None,
        *,
        message_id: str | None = None,
    ) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.settings.email_from
        msg["To"] = self.settings.email_to
        if message_id:
            msg["Message-ID"] = message_id
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        try:
            if self.settings.smtp_tls_mode == "ssl":
                with smtplib.SMTP_SSL(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=20,
                ) as smtp:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                    smtp.sendmail(
                        self.settings.email_from,
                        [self.settings.email_to],
                        msg.as_string(),
                    )
            elif self.settings.smtp_tls_mode == "starttls":
                with smtplib.SMTP(
                    self.settings.smtp_host,
                    self.settings.smtp_port,
                    timeout=20,
                ) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                    smtp.sendmail(
                        self.settings.email_from,
                        [self.settings.email_to],
                        msg.as_string(),
                    )
            else:
                raise EmailSendError("Invalid SMTP_TLS_MODE")
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            raise EmailSendError("SMTP send failed") from exc
