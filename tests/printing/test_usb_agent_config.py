"""Offline configuration/binding tests: no Windows printing or network."""
import json
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from services.printing import usb_agent_config as config
from services.printing import grit_windows_usb_agent as agent


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.data=dict(url=config.BASE_URL,printer=config.PRINTER,token='SYNTHETIC_'*5,
                       stand_id='a'*64,ticket_id='b'*32)
        self.ping=dict(stand_id='a'*64,ticket_id='b'*32,waiting=1,device=config.PRINTER)

    def test_valid_configuration(self):
        self.assertEqual(config.validate(self.data),self.data)

    def test_remote_host_default_printer_and_malformed_bindings_refused(self):
        for key,value in [('url','https://example.com'),('url','http://localhost:18769'),
                          ('printer','default'),('token','short'),('ticket_id','bad'),('stand_id','bad')]:
            with self.subTest(key=key,value=value), self.assertRaises(ValueError):
                config.validate(dict(self.data,**{key:value}))

    def test_bound_claim_passes_exact_ticket_and_stand(self):
        api=Mock(return_value=dict(state='CLAIMED',ticket_id='b'*32))
        config.bind_api(api,self.data)('POST','/claim',{})
        api.assert_called_once_with('POST','/claim',dict(ticket_id='b'*32,stand_id='a'*64))

    def test_other_stand_printer_or_approval_refused(self):
        for key,value in [('stand_id','c'*64),('device','other'),('waiting',0),
                          ('waiting',2),('ticket_id','c'*32)]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.bind_api(Mock(return_value=dict(self.ping,**{key:value})),self.data)('GET','/ping')

    def test_wrong_claim_ticket_refused(self):
        with self.assertRaises(ValueError):
            config.bind_api(Mock(return_value=dict(state='CLAIMED',ticket_id='c'*32)),self.data)('POST','/claim')

    def test_ssh_enrollment_does_not_put_token_in_argv(self):
        result=Mock(returncode=0,stdout=json.dumps(self.data).encode())
        with patch.object(config.subprocess,'run',return_value=result) as run:
            self.assertEqual(config.enrollment_from_ssh(),self.data)
        argv=run.call_args.args[0]
        self.assertNotIn(self.data['token'],' '.join(argv))
        self.assertEqual(argv[:2],['ssh','grit'])

    def test_bad_ssh_response_does_not_echo_secrets(self):
        with patch.object(config.subprocess,'run',return_value=Mock(returncode=0,stdout=b'SECRET_BAD_RESPONSE')):
            with self.assertRaises(ValueError) as error:
                config.enrollment_from_ssh()
        self.assertNotIn('SECRET_BAD_RESPONSE',str(error.exception))

    def test_unenrolled_main_cannot_fallback_to_env_token(self):
        with patch.object(config,'load',side_effect=ValueError('Not enrolled')), \
             patch.object(config,'tunnel',return_value=nullcontext()), \
             patch.object(agent,'make_api_call') as api, patch.object(agent,'_spooler') as spooler:
            self.assertEqual(agent.main(['--check']),1)
        api.assert_not_called()
        spooler.assert_not_called()


if __name__=='__main__':
    unittest.main()
