"""Only local db/restore_check_*; always strip external service credentials."""
import os
import re
import sys
from urllib.parse import quote


def database_url():
    name=os.getenv('GRIT_TRIAL_DATABASE','restore_check_trial')
    if not re.fullmatch(r'restore_check_[a-z0-9_]{1,45}',name):
        raise RuntimeError('Trial database must be restore_check_*')
    return 'postgresql://grit:'+quote(os.environ['POSTGRES_PASSWORD'],safe='')+'@db:5432/'+name


if __name__=='__main__':
    os.environ['DATABASE_URL']=database_url()
    os.environ['WMS_REQUIRE_POSTGRES']='1'
    os.environ['WMS_MIGRATE_SQLITE_FROM_S3']='0'
    for key in ('MOYSKLAD_TOKEN','WB_API_TOKEN','ONIX_PRINT_AGENT_TOKEN','YC_S3_BUCKET','YC_S3_ACCESS_KEY_ID','YC_S3_SECRET_ACCESS_KEY'):
        os.environ.pop(key,None)
    os.execvp(sys.argv[1],sys.argv[1:])
