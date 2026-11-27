"""PR211 release gates: no implicit deployment, no default-enabled printing.
Synthetic stand checks only; never connects to WB or the physical printer.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from services.printing.pr211_registration import register_pr211_isolated

ROOT=Path(__file__).resolve().parents[2]


class ReleaseGates(unittest.TestCase):
    def test_default_app_printing_switch_is_off(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertIs(register_pr211_isolated(None),False)

    def test_invalid_explicit_mode_fails_closed(self):
        with patch.dict(os.environ,{'GRIT_PR211_ORIGINAL_FLOW':'yes'},clear=True):
            with self.assertRaisesRegex(RuntimeError,'UNRECOGNIZED'):
                register_pr211_isolated(None)

    def test_synthetic_flag_alone_cannot_enable_printing(self):
        with patch.dict(os.environ,{
            'GRIT_PR211_ORIGINAL_FLOW':'YES_ISOLATED_SYNTHETIC',
        },clear=True):
            with self.assertRaisesRegex(RuntimeError,'SYNTHETIC_DATABASE_GATE'):
                register_pr211_isolated(None)

    def test_synthetic_gate_rejects_production_url(self):
        with patch.dict(os.environ,{
            'GRIT_PR211_ORIGINAL_FLOW':'YES_ISOLATED_SYNTHETIC',
            'GRIT_SYNTHETIC_PG_TEST':'YES_ISOLATED',
            'PR211_SYNTHETIC_DATABASE_URL':'postgresql://production/db',
        },clear=True):
            with self.assertRaisesRegex(RuntimeError,'DISPOSABLE_DATABASE'):
                register_pr211_isolated(None)

    def test_key_required_from_separate_environment(self):
        with patch.dict(os.environ,{
            'GRIT_PR211_ORIGINAL_FLOW':'YES_ISOLATED_SYNTHETIC',
            'GRIT_SYNTHETIC_PG_TEST':'YES_ISOLATED',
            'PR211_SYNTHETIC_DATABASE_URL':
              'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_test',
        },clear=True):
            with self.assertRaisesRegex(RuntimeError,'EPHEMERAL_AES_KEY'):
                register_pr211_isolated(None)

    def test_proposed_migration_is_additive_and_not_automatic(self):
        sql=(ROOT/'deploy/migrations/pr211_original_wb_print_additive.sql').read_text()
        upper=sql.upper()
        for forbidden in ('DROP TABLE','TRUNCATE','DELETE FROM','ALTER TABLE',
                          'DROP INDEX','UPDATE WB_FBS_'):
            self.assertNotIn(forbidden,upper)
        self.assertIn('CREATE TABLE IF NOT EXISTS',upper)
        self.assertIn("DEFAULT FALSE",upper)
        self.assertIn('PREREQUISITES MISSING',upper)

    def test_no_real_production_registration_when_flag_unset(self):
        s=(ROOT/'app.py').read_text()
        self.assertIn('register_pr211_isolated(app)',s)
        self.assertNotIn('GRIT_WB_REAL_PRINTING=1',s)


if __name__=='__main__':
    unittest.main()
