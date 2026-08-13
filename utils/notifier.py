# Email notification utilities for completion summaries and error alerts.
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import Config


class Notifier:
    """Sends scraper completion and failure notifications via Gmail SMTP."""

    def __init__(self, config: Config):
        """Store runtime config used for email sender and recipients."""
        self.config = config

    def send_completion_summary(
        self,
        total_scraped: int,
        total_matched: int,
        total_written: int,
        llm_calls_made: int,
        duplicates_skipped: int,
        errors: int,
        run_mode: str,
    ) -> None:
        """Send HTML completion summary email and never raise on failure."""
        try:
            app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
            if not app_password:
                print("Email notifications disabled — GMAIL_APP_PASSWORD not set")
                return

            today = datetime.now().strftime("%Y-%m-%d")
            estimated_cost = llm_calls_made * 0.01
            subject = (
                f"GovRisk Scraper Run Complete — {today} "
                f"({total_written} new opportunities)"
            )

            dry_run_note = (
                "<p><strong>Note:</strong> dry_run mode — nothing written.</p>"
                if run_mode == "dry_run"
                else ""
            )

            html_body = f"""
            <html>
              <body>
                <h3>GovRisk Scraper Run Summary</h3>
                <table border="1" cellpadding="6" cellspacing="0">
                  <tr><td>Total pages scraped</td><td>{total_scraped}</td></tr>
                  <tr><td>Keyword matches found</td><td>{total_matched}</td></tr>
                  <tr><td>New records written</td><td>{total_written}</td></tr>
                  <tr><td>LLM calls made</td><td>{llm_calls_made}</td></tr>
                  <tr><td>Duplicates skipped</td><td>{duplicates_skipped}</td></tr>
                  <tr><td>Errors encountered</td><td>{errors}</td></tr>
                  <tr><td>Run mode</td><td>{run_mode}</td></tr>
                  <tr><td>Estimated LLM cost</td><td>${estimated_cost:.2f}</td></tr>
                </table>
                <p>Link to data store for review.</p>
                {dry_run_note}
              </body>
            </html>
            """

            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = self.config.notification_email
            message["To"] = self.config.notification_email
            message.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.config.notification_email, app_password)
                server.sendmail(
                    self.config.notification_email,
                    [self.config.notification_email],
                    message.as_string(),
                )
        except Exception as exc:
            print(f"Failed to send completion summary email: {exc}")

    def send_error_alert(self, error_message: str, component: str) -> None:
        """Send plain-text error alert to admin address and never raise."""
        try:
            app_password = os.getenv("GMAIL_APP_PASSWORD", "").strip()
            if not app_password:
                print("Email notifications disabled — GMAIL_APP_PASSWORD not set")
                return

            timestamp = datetime.now().isoformat()
            subject = f"GovRisk Scraper ERROR — {component}"
            body = (
                f"Timestamp: {timestamp}\n"
                f"Component: {component}\n\n"
                f"Error message:\n{error_message}"
            )

            message = MIMEMultipart("alternative")
            message["Subject"] = subject
            message["From"] = self.config.notification_email
            message["To"] = self.config.admin_alert_email
            message.attach(MIMEText(body, "plain"))

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.config.notification_email, app_password)
                server.sendmail(
                    self.config.notification_email,
                    [self.config.admin_alert_email],
                    message.as_string(),
                )
        except Exception as exc:
            print(f"Failed to send error alert email: {exc}")
