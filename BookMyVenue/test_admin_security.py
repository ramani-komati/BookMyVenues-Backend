"""Django admin login: lockout after repeated failures, success unaffected."""
from django.core.cache import cache
from django.test import TestCase, override_settings

from accounts.models import User


# Plain static storage: the test run has no collectstatic manifest.
@override_settings(ADMIN_LOGIN_MAX_FAILURES=3, ADMIN_LOGIN_LOCK_SECONDS=60, STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
})
class AdminLoginLockoutTests(TestCase):
    URL = '/admin/login/'

    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user(phone='9444400001', name='Staff', password='right-pass')
        User.objects.filter(pk=self.staff.pk).update(is_staff=True, is_superuser=True)

    def attempt(self, password):
        return self.client.post(self.URL, {'username': '9444400001', 'password': password})

    def test_locks_after_repeated_failures(self):
        for _ in range(3):
            self.assertEqual(self.attempt('wrong').status_code, 200)   # form re-shown
        self.assertEqual(self.attempt('wrong').status_code, 429)
        self.assertEqual(self.attempt('right-pass').status_code, 429)  # locked even if right

    def test_success_clears_the_counter(self):
        self.attempt('wrong')
        self.attempt('wrong')
        self.assertEqual(self.attempt('right-pass').status_code, 302)  # signed in
        self.client.logout()
        for _ in range(3):
            self.assertEqual(self.attempt('wrong').status_code, 200)

    def test_login_page_still_opens(self):
        self.assertEqual(self.client.get(self.URL).status_code, 200)
