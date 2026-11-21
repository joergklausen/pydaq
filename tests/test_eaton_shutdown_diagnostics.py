from pathlib import Path
import importlib.util
import sys
import types
import pytest

# Offline tests without requiring the full pydaq runtime or live UPS.
root = Path(__file__).resolve().parents[1]
sys.modules.setdefault('pydaq', types.ModuleType('pydaq'))
sys.modules.setdefault('pydaq.instruments', types.ModuleType('pydaq.instruments'))
eaton = types.ModuleType('pydaq.instruments.eaton')
eaton._read_secret = lambda path: ''
sys.modules['pydaq.instruments.eaton'] = eaton
spec = importlib.util.spec_from_file_location('diagnostics',root/'pydaq/utils/eaton_shutdown_diagnostics.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def test_correct_low_batt_oid():
    assert m.OBJECTS['low_battery_warning_min'][0] == '1.3.6.1.2.1.33.1.9.7.0'

def test_every_oid_is_queried_once():
    names=[name for group in m.GROUPS for name in group]
    assert set(names)==set(m.OBJECTS)
    assert len(names)==len(set(names))

def test_missing_oid_not_treated_as_zero():
    names=('low_battery_warning_min','battery_capacity_pct')
    values=m.decode_response(names,{m.OBJECTS[names[0]][0]:'No Such Instance currently exists at this OID',m.OBJECTS[names[1]][0]:'96'})
    assert values[names[0]] is None
    assert values[names[1]]=='96'

def test_config_uses_existing_snmp_entry(tmp_path):
    p=tmp_path/'config.yml'
    p.write_text('instruments:\n  eaton:\n    io:\n      kind: snmp\n      host: 192.0.2.1\n      user: admin\n      security_level: noAuthNoPriv\n')
    assert m.read_io(p)['host']=='192.0.2.1'

def test_no_claim_of_ebm_count():
    report=m.format_report({'low_battery_warning_min':'3'}, {}, 'today')
    assert '3 [minutes; configured threshold]' in report
    assert 'No EBM count is inferred' in report
