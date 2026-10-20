import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haus_kettle.credentials import CredentialsError, api, load, refresh, save, validate
from haus_kettle.model import MODEL


class CredentialsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.credentials = {"host": "192.168.1.20", "token": "a" * 32, "did": "123", "model": MODEL}

    def test_validated_local_credentials_require_no_cloud_account(self):
        with patch("haus_kettle.credentials.api", side_effect=AssertionError("No cloud")):
            self.assertEqual(
                load({"source": "local", "device_id": "123", **self.credentials}, self.path),
                self.credentials,
            )

    def test_import_rejects_ambiguous_devices_without_overwriting_existing_cache(self):
        save(self.path, self.credentials)
        with patch(
            "haus_kettle.credentials.api",
            return_value={"service_response": {"list": [self.credentials] * 2}},
        ):
            with self.assertRaises(CredentialsError):
                refresh({}, self.path)
        self.assertEqual(json.loads(self.path.read_text()), self.credentials)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_cached_token_survives_cloud_session_expiry_and_never_prints_it(self):
        save(self.path, self.credentials)
        with patch("haus_kettle.credentials.api", side_effect=CredentialsError("Reconnect Xiaomi")):
            self.assertEqual(load({}, self.path), self.credentials)
            with self.assertRaises(CredentialsError):
                refresh({}, self.path, renew=True)
        self.assertEqual(json.loads(self.path.read_text()), self.credentials)

    def test_invalid_credentials_do_not_reach_transport(self):
        for key, value in [
            ("host", "8.8.8.8"),
            ("host", "127.0.0.1"),
            ("token", "oops"),
            ("did", "a/b"),
            ("model", "unsupported"),
        ]:
            with self.assertRaises(CredentialsError):
                validate({**self.credentials, key: value})

    def test_ha_services_without_response_return_a_list_not_an_object(self):
        with (
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "synthetic-test-token"}),
            patch("urllib.request.urlopen", return_value=io.StringIO("[]")),
        ):
            self.assertEqual(api("/core/api/services/xiaomi_miot/renew_devices", {}), [])
        with patch(
            "haus_kettle.credentials.api",
            side_effect=[[], {"service_response": {"list": [self.credentials]}}],
        ):
            self.assertEqual(refresh({}, self.path, renew=True), self.credentials)

    def test_unexpected_import_response_is_a_recoverable_configuration_error(self):
        for result in [None, [], {"service_response": None}]:
            with (
                patch("haus_kettle.credentials.api", return_value=result),
                self.assertRaises(CredentialsError),
            ):
                refresh({}, self.path)
