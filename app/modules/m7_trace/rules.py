from collections import Counter
import hashlib, json
from app.normalize import serial
from app.errors import DomainError


def audit(rows, quarantine, input_count, accepted_lines=None):
    """行数平衡：采信回执行 + 隔离回执行 = 逻辑输入行。

    rows 是车次事实；一张事实可能由多张回执合成，所以传入
    accepted_lines（本轮实际贡献字段的回执行数）时按回执计。
    """
    accepted = len(rows) if accepted_lines is None else accepted_lines
    if accepted + len(quarantine) != input_count:
        raise DomainError('INCOMPLETE', '行数平衡失败')
    if len({r.get('trip') or r['id'] for r in rows}) != len(rows):
        raise DomainError('CONFLICT', '重复车次未裁决')
    fingerprint = hashlib.sha256(json.dumps(serial(rows), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'count': len(rows), 'accepted_lines': accepted, 'quarantined': len(quarantine),
            'input_count': input_count, 'fingerprint': fingerprint,
            'causes': dict(Counter(r['code'] for r in quarantine))}
