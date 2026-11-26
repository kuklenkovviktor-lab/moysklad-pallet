"""Offline, per-module regression runner (unittest and plain test functions)."""
import argparse, io, json, os, shutil, subprocess, sys, tarfile, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
FILES = ['test_grit_unified_theme.py', 'test_grit_dashboard_theme.py',
 'test_grit_dashboard_polish.py', 'test_grit_brand_public_assets.py',
 'test_integrations_manager_middleware.py', 'test_integrations_navigation.py',
 'tests/test_grit_dark_design_policy.py', 'tests/test_mobile_pwa_navigation.py',
 'tests/test_badge_theme_consistency.py', 'tests/test_manager_badge_route_auth.py',
 'tests/test_receiving_confirmation.py', 'tests/test_mobile_receiving_resume.py',
 'tests/test_wms_operation_times.py', 'tests/test_attendance.py']
RESTORED_FILES = FILES + ['tests/test_receiving_attendance_access.py']
TEST_CHANGES = ['test_grit_dashboard_theme.py', 'test_grit_dashboard_polish.py',
 'tests/test_mobile_pwa_navigation.py', 'tests/test_receiving_confirmation.py',
 'tests/test_mobile_receiving_resume.py', 'tests/attendance_test_support.py',
 'tests/test_receiving_attendance_access.py']
CHILD = r'''
import sys,os,runpy,types,unittest,io,socket,tempfile,json
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[os.getcwd(),str(Path.cwd()/'tests')]+[p for p in os.environ.get('PYTHONPATH','').split(os.pathsep) if p]
with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'DATABASE_URL':'','WMS_REQUIRE_POSTGRES':'0','WMS_DB_PATH':str(Path(tmp)/'synthetic.sqlite'),'SECRET_KEY':'synthetic','WMS_BADGE_SECRET':'synthetic','WMS_TERMINAL_SECRET':'synthetic','MOYSKLAD_TOKEN':'synthetic-offline-only','WB_API_TOKEN':'','WB_CONTENT_TOKEN':''}), patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
 m=types.ModuleType('isolated');m.__dict__.update(runpy.run_path(sys.argv[1]))
 suite=unittest.defaultTestLoader.loadTestsFromModule(m)
 for name,value in vars(m).items():
  if name.startswith('test_') and callable(value):suite.addTest(unittest.FunctionTestCase(value))
 result=unittest.TextTestRunner(stream=io.StringIO()).run(suite)
 print('RESULT '+json.dumps({'file':sys.argv[1],'run':result.testsRun,'fail':[(x.id(),e[-600:]) for x,e in result.failures],'error':[(x.id(),e[-600:]) for x,e in result.errors],'skip':len(result.skipped)}))
'''

def run(root, files=RESTORED_FILES):
    results=[]
    for file in files:
        r=subprocess.run([sys.executable,'-c',CHILD,file],cwd=root,capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,PYTHONUTF8='1'),timeout=90)
        lines=[x[7:] for x in r.stdout.splitlines() if x.startswith('RESULT ')]
        if not lines: raise RuntimeError(file+': '+r.stderr[-1200:])
        result=json.loads(lines[-1]);results.append(result)
        print(json.dumps(result,ensure_ascii=False),flush=True)
    print('TOTAL',sum(x['run'] for x in results),'FAIL',sum(len(x['fail']) for x in results),'ERROR',sum(len(x['error']) for x in results),'SKIP',sum(x['skip'] for x in results),flush=True)
    return results

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compare-base', help='Run original tests, then restored tests on this git SHA')
    args = parser.parse_args()
    if args.compare_base:
        with tempfile.TemporaryDirectory(prefix='grit-regression-base-') as tmp:
            data = subprocess.check_output(['git', 'archive', args.compare_base], cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                archive.extractall(tmp, filter='data')
            print('ORIGINAL TESTS / BASE', args.compare_base, flush=True)
            run(tmp, FILES)
            for file in TEST_CHANGES:
                shutil.copyfile(ROOT / file, Path(tmp) / file)
            print('RESTORED TESTS / BASE (only test files overlaid)', flush=True)
            base_results = run(tmp)
    print('RESTORED TESTS / CANDIDATE', flush=True)
    results = run(ROOT)
    checked = results + (base_results if args.compare_base else [])
    sys.exit(bool(any(r['fail'] or r['error'] or r['skip'] for r in checked)))
