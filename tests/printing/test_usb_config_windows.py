"""Real current-user DPAPI/ACL, temporary synthetic config, NO printer access."""
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from services.printing import usb_agent_config as config


@unittest.skipUnless(sys.platform == 'win32','Windows DPAPI required')
class WindowsConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='grit-dpapi-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'private'/'binding.dpapi'
        p=patch.object(config,'config_path',return_value=self.path)
        p.start()
        self.addCleanup(p.stop)
        self.data=dict(url=config.BASE_URL,printer=config.PRINTER,token='synthetic-only-'*4,
                       stand_id='a'*64,ticket_id='b'*32)

    def test_encrypted_roundtrip_and_private_acl(self):
        import win32api,win32con,win32security as sec
        config.save(self.data)
        self.assertNotIn(self.data['token'].encode(),self.path.read_bytes())
        self.assertEqual(config.load(),self.data)
        sd=sec.GetNamedSecurityInfo(str(self.path),sec.SE_FILE_OBJECT,sec.DACL_SECURITY_INFORMATION)
        acl=sd.GetSecurityDescriptorDacl()
        self.assertEqual(acl.GetAceCount(),1)
        token=sec.OpenProcessToken(win32api.GetCurrentProcess(),win32con.TOKEN_QUERY)
        try:
            sid=sec.GetTokenInformation(token,sec.TokenUser)[0]
        finally:
            token.Close()
        self.assertEqual(acl.GetAce(0)[2],sid)

    def test_corrupt_config_fails_closed(self):
        config.save(self.data)
        self.path.write_bytes(b'corrupt synthetic ciphertext')
        with self.assertRaises(ValueError):
            config.load()


if __name__=='__main__':
    unittest.main()
