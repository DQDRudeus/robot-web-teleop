"""Credential defaults for a public installation, without devices."""
import unittest
from unittest.mock import patch
import test_control

backend = test_control.backend


class PublicCredentialTests(unittest.TestCase):
    def test_missing_advanced_credential_refuses_empty_and_arbitrary_input(self):
        with patch.object(backend, 'TOKEN_RECORD', None), patch.object(backend, 'OPERATOR_TOKEN', ''):
            self.assertFalse(backend.valid_operator_token(''))
            self.assertFalse(backend.valid_operator_token('unconfigured'))

    def test_former_default_is_not_an_advanced_credential(self):
        with patch.object(backend, 'TOKEN_RECORD', None), patch.object(backend, 'OPERATOR_TOKEN', ''):
            self.assertFalse(backend.valid_operator_token('hkcamera'))

    def test_configured_credential_still_works(self):
        with patch.object(backend, 'TOKEN_RECORD', None), patch.object(backend, 'OPERATOR_TOKEN', 'fixture-only'):
            self.assertTrue(backend.valid_operator_token('fixture-only'))
            self.assertFalse(backend.valid_operator_token(''))


class PublicRoleTests(unittest.TestCase):
    def test_unconfigured_test_role_cannot_claim_control(self):
        fixture = test_control.ControlTests(methodName='runTest')
        fixture.setUp()
        try:
            with patch.object(backend, 'TEST_OPERATOR_TOKEN', ''):
                with fixture.client.websocket_connect('/ws/control') as ws:
                    test_control.receive(ws, 'hello')
                    ws.send_json({'type': 'claim_control', 'operator_token': 'test'})
                    result = test_control.receive(ws, 'control')
                    self.assertFalse(result['ok'])
        finally:
            fixture.tearDown()
