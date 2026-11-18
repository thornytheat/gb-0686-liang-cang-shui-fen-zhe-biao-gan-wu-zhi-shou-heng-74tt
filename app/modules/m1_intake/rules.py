from app.normalize import rows_from

# 只要求每行有磅单号与版本：地磅补传和化验复验各自只带本方字段，
# 不能因为缺少另一来源的列而整份文件被拒。
from app.errors import DomainError
from app.source_contract import TRIP_COLUMNS

REQUIRED = ['version']


def receive(raw):
    result = rows_from(raw, REQUIRED)
    header = result.get('header', [])
    if 'id' not in header and not set(TRIP_COLUMNS) & set(header):
        raise DomainError('HEADER', '字段名不符合契约')
    for row in result['raw']:
        if not (row.get('id') or '').strip() and not any((row.get(k) or '').strip() for k in TRIP_COLUMNS):
            row['_missing_key'] = True
    return result
