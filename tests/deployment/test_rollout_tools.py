import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest
from psycopg.conninfo import conninfo_to_dict

ROOT=Path(__file__).resolve().parents[2]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'deploy/migration'/f'{name}.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.m=load('migration_tool')
        self.baseline={'server_version':170011,'tables':{'employees':{'rows':5,'sha256':'same','columns':[]}},'integrity':{'orphan_scans':0},'extensions':[['plpgsql','1.0']]}

    def new_table(self):
        return {'rows':0,'sha256':hashlib.sha256(b'').hexdigest(),
                'columns':[[name,'text','text','NO',None] for name in ('request_id','scan_id','shift_id','employee_id','barcode','product_barcode')],
                'column_details':[[name,'text',True,'','',None,'pg_catalog','default'] for name in ('request_id','scan_id','shift_id','employee_id','barcode','product_barcode')],
                'constraints':[['fbs_scan_requests_pkey','PRIMARY KEY (request_id)',True]],
                'indexes':[['fbs_scan_requests_pkey','CREATE UNIQUE INDEX fbs_scan_requests_pkey ON public.fbs_scan_requests USING btree (request_id)']]}

    def test_exact_new_empty_ledger_allowed_but_strict_restore_comparison_unchanged(self):
        after=copy.deepcopy(self.baseline);after['tables']['fbs_scan_requests']=self.new_table();after['integrity']['orphan_fbs_requests']=0
        self.assertEqual(self.m.compare_rollout(self.baseline,after),[])
        self.assertIn('tables mismatch',self.m.compare(self.baseline,after))

    def test_existing_content_change_rejected_even_with_same_counts(self):
        after=copy.deepcopy(self.baseline);after['tables']['employees']['sha256']='changed'
        self.assertTrue(self.m.compare_rollout(self.baseline,after))

    def test_unknown_or_nonempty_or_wrong_schema_addition_rejected(self):
        for name,table in [('unknown',self.new_table()),('fbs_scan_requests',{**self.new_table(),'rows':1}),('fbs_scan_requests',{**self.new_table(),'columns':[]})]:
            after=copy.deepcopy(self.baseline);after['tables'][name]=table
            self.assertTrue(self.m.compare_rollout(self.baseline,after))

    def test_server_or_extension_change_rejected(self):
        for field,value in [('server_version',170012),('extensions',[])]:
            after=copy.deepcopy(self.baseline);after[field]=value
            self.assertTrue(self.m.compare_rollout(self.baseline,after))

    def test_local_dsn_preserves_special_password_without_uri_interpolation(self):
        environment=dict(PGHOST='db',PGPORT='5432',PGUSER='grit',PGDATABASE='grit',PGPASSWORD="test-only:@/ '$")
        result=conninfo_to_dict(load('ops_entrypoint').local_dsn(environment))
        self.assertEqual(result['password'],environment['PGPASSWORD']);self.assertEqual(result['dbname'],'grit')

    def test_ops_cannot_be_redirected_to_render(self):
        environment=dict(PGHOST='db',PGPORT='5432',PGUSER='grit',PGDATABASE='grit',PGPASSWORD='test-only')
        for name,value in [('PGHOST','render.example.invalid'),('PGDATABASE','production'),('PGUSER','other')]:
            with self.assertRaises(ValueError): load('ops_entrypoint').local_dsn({**environment,name:value})
