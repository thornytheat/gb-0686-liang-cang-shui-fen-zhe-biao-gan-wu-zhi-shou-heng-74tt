from .rules import settings, coerce
from app.errors import DomainError
from app.reasons import rejected


def execute(ctx):
    ctx['rules'] = settings(ctx['params'])
    good = []
    for row in ctx['records']:
        try:
            good.append(coerce(row))
        except DomainError as exc:
            ctx['quarantine'].append(rejected(row, exc.code, exc.message))
    ctx['records'] = good
    ctx['stages'].append({'title': '水杂契约', 'rows': good, 'metrics': {
        '有效': len(good), '隔离': len(ctx['quarantine']),
        '标准水分 %': str(ctx['rules']['standard_moisture']),
        '地磅回执': sum(r.get('_source') == 'scale' for r in good),
        '化验回执': sum(r.get('_source') == 'lab' for r in good),
    }})
    return ctx
