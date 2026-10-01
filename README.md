## Invoice Generator

Set these environment variables to email invoices after creation and when the user selects **Email invoice**. The app loads them from a local `.env` file:

```text
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_FROM=napoleonchikadibia@gmail.com
SMTP_USERNAME=napoleonchikadibia@gmail.com
SMTP_PASSWORD=your-smtp-password
```

For Gmail, enable 2-Step Verification and create a Google app password. Use that 16-character app password as `SMTP_PASSWORD`; do not use your normal Gmail password.

The app stores the customer's email with each invoice. If SMTP is not configured, the invoice is still created and the screen explains that delivery is unavailable.
