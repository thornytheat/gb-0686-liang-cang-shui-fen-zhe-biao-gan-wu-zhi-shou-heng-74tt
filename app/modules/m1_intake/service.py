from .rules import receive
from app.reasons import rejected
from app.source_contract import raw_fields


def execute(ctx):
    parsed = receive(ctx['source'])
    ctx.update(parsed)
    ctx['records'] = []
    for row in ctx['raw']:
        if row.get('_missing_key'):
            ctx['quarantine'].append(rejected(row, 'MISSING', '磅单号或车次为空'))
        else:
            # 保留原始单元格文本：后续归一不覆盖证据里的原值。
            row['_raw_fields'] = raw_fields(row)
            ctx['records'].append(row)
    ctx['stages'].append({'title': '磅单采信', 'rows': ctx['records'], 'metrics': {
        '逻辑行': ctx['input_count'], '采信': len(ctx['records']), '隔离': len(ctx['quarantine'])}})
    return ctx
