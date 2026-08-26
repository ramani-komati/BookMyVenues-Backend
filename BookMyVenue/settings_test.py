"""
Test settings — identical to production settings except the test database
gets its own name.

Why: the default `test_postgres` can be left locked by an orphaned pooler
session after an interrupted run, which blocks every later test run with
"database is being accessed by other users". A dedicated name sidesteps that
without ever touching the real database.
"""
from .settings import *  # noqa: F401,F403
from .settings import DATABASES

DATABASES['default'].setdefault('TEST', {})
DATABASES['default']['TEST']['NAME'] = 'test_bmv_suite'

# The test client talks to this host.
ALLOWED_HOSTS = ['testserver', 'localhost', '127.0.0.1']

# SAFETY: blank every outbound provider credential so a test can never reach a
# real service — no stray SMS, no email, no gateway call, no spend. Tests that
# exercise a provider supply their own fake keys with override_settings.
FAST2SMS_API_KEY = ''
TWOFACTOR_API_KEY = ''
RESEND_API_KEY = ''
RAZORPAY_KEY_ID = ''
RAZORPAY_KEY_SECRET = ''
RAZORPAY_WEBHOOK_SECRET = ''
