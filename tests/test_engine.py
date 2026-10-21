import copy
from pathlib import Path
import pytest
import yaml
from ingest.core import (config_digest,decode,digest,extract,parse_records,transform,valid_abn,valid_acn)
from ingest.cli import require_approval
from ingest.resolve import resolve,profiles

ONTOLOGY=yaml.safe_load((Path(__file__).parents[1]/'firmable_ontology.yaml').read_text())
CONFIG={'parser':{'format':'csv'},'minimum_field_confidence':.8,'mappings':[
    {'source_field':'ABN','canonical_field':'entity.abn','transforms':['abn'],'confidence':.95},
    {'source_field':'Name','canonical_field':'entity.legal_name','transforms':['strip'],'confidence':.9},
    {'source_field':'Address','canonical_field':'address.full','transforms':['strip'],'confidence':.9}],
    'review':{'status':'pending'}}


def source(sid):
    return {'source_id':sid,'resource_sha256':digest(sid),'resource_url':'https://example.org/'+sid,
            'observed_at':'2026-01-01T00:00:00Z','observed_at_basis':'resource_last_modified_proxy',
            'downloaded_at':'2026-10-01T00:00:00Z','licence':'CC BY 4.0','source_reliability':.8}


def observations(sid,name='Example Pty Ltd',address='1 Road, QLD'):
    return extract([{'ABN':'51 824 753 556','Name':name,'Address':address}],CONFIG,source(sid),ONTOLOGY)[0]


def test_checksum_vectors_and_no_inferred_suffix():
    assert valid_abn('51824753556')
    assert not valid_abn('51824753557')
    assert not valid_abn('00000000000')
    assert valid_acn('004085616')
    assert not valid_acn('004085617')
    assert not valid_acn('000000000')
    with pytest.raises(ValueError): transform('ABN: 51 824 753 556',['abn'])
    with pytest.raises(ValueError): transform('51824753556;12345678901',['digits','abn'])


def test_utf8_prefix_and_bom_detection():
    text='Supplier,Address\nCompany,Café'
    decoded,encoding=decode(text.encode()[:-1])
    assert encoding=='utf-8-sig'
    assert decoded.startswith('Supplier')
    assert decode(text.encode('utf-16'))[1]=='utf-16'


def test_multiline_csv_and_nested_json_share_one_engine():
    rows=parse_records(b'ABN,Name,Address\n51824753556,"Example\nPty Ltd",Road\n',{'format':'csv'})
    import json
    wrapped=json.dumps({'result':{'records':rows}}).encode()
    assert parse_records(wrapped,{'format':'json','record_path':'result.records'})==rows
    a,_=extract(rows,CONFIG,source('a'),ONTOLOGY)
    assert len(a)==3
    assert all(o['observed_at']!=o['ingested_at'] for o in a)
    assert all(o['licence']=='CC BY 4.0' for o in a)
    assert all('link_confidence' not in o['confidence'] for o in a)


def test_invalid_identifier_is_quarantined_not_emitted():
    obs,errors=extract([{'ABN':'51824753557','Name':'Example','Address':''}],CONFIG,source('a'),ONTOLOGY)
    assert not any(o['canonical_field']=='entity.abn' for o in obs)
    assert any('checksum' in e['reason'] for e in errors)


def test_duplicate_rows_remain_traceable_and_ids_replay_stably():
    row={'ABN':'51824753556','Name':'Example','Address':'Road'}
    a,_=extract([row,row],CONFIG,source('a'),ONTOLOGY)
    b,_=extract([row,row],CONFIG,source('a'),ONTOLOGY)
    assert [o['observation_id'] for o in a]==[o['observation_id'] for o in b]
    assert len({o['source_record_id'] for o in a})==2


def test_review_hash_binds_exact_mapping_and_ontology():
    config=copy.deepcopy(CONFIG)
    with pytest.raises(ValueError): require_approval(config)
    config['review']={'status':'approved','approved_config_sha256':config_digest(config)}
    require_approval(config)
    config['mappings'][0]['source_field']='Wrong'
    with pytest.raises(ValueError):require_approval(config)
    config=copy.deepcopy(CONFIG);config['ontology_sha256']='changed'
    with pytest.raises(ValueError,match='Ontology'):
        extract([{'ABN':'51824753556','Name':'Example','Address':'Road'}],config,source('a'),ONTOLOGY)


def test_model_cannot_lower_engine_confidence_floor():
    config=copy.deepcopy(CONFIG);config['minimum_field_confidence']=.6
    with pytest.raises(ValueError,match='policy floor'):
        extract([{'ABN':'51824753556','Name':'Example','Address':'Road'}],config,source('a'),ONTOLOGY)


def test_regex_refuses_multiple_supplier_identifiers():
    steps=[{'op':'regex','pattern':r'ABN:\s*([0-9 ]{11,15})','group':1},'abn']
    assert transform('Example, ABN: 51 824 753 556',steps)=='51824753556'
    with pytest.raises(ValueError,match='exactly one'):
        transform('ABN: 51 824 753 556\nABN: 51 824 753 556',steps)


def test_compound_identifier_claims_get_separate_entities_without_name_pairing():
    config=copy.deepcopy(CONFIG)
    config['mappings']=[{'source_field':'ABNs','canonical_field':'entity.abn','confidence':.95,
                         'transforms':[{'op':'split','delimiter':';'},'abn'],'separate_entities':True}]
    rows=[{'ABNs':'51 824 753 556;83 914 571 673'}]
    obs,errors=extract(rows,config,source('a'),ONTOLOGY)
    assert not errors
    assert len(obs)==2 and len({o['source_record_id'] for o in obs})==2
    assert all(o['provenance']['parent_source_record_id'] for o in obs)
    assert all('legal_name' not in o['entity'] for o in obs)


def test_cross_source_links_abstain_and_profiles_show_conflicts(tmp_path):
    obs=observations('a')+observations('b',address='2 Different Road, NSW')
    links=resolve(obs,tmp_path)
    assert len(links)==1
    assert links[0]['canonical_entity_key']=='abn:51824753556'
    result=profiles(obs,links,tmp_path)
    field=result[0]['fields']['address.full']
    assert field['conflict'] and field['alternatives']
    assert field['confidence']['field_confidence']==.7
    assert field['provenance']
    assert not resolve(observations('a')+observations('a'),tmp_path)
    assert not resolve(observations('a')+observations('b',name='Unrelated Different Organisation'),tmp_path)


def test_name_only_never_auto_links(tmp_path):
    obs=[o for o in observations('a')+observations('b') if o['canonical_field']!='entity.abn']
    assert resolve(obs,tmp_path)==[]


def test_explicit_acn_abn_bridge_and_contradiction_abstention(tmp_path):
    config=copy.deepcopy(CONFIG)
    config['mappings'].append({'source_field':'ACN','canonical_field':'entity.acn','transforms':['acn'],'confidence':.95})
    a,_=extract([{'ABN':'51824753556','ACN':'004085616','Name':'Example Pty Ltd','Address':'Road'}],config,source('a'),ONTOLOGY)
    b,_=extract([{'ABN':'','ACN':'004085616','Name':'Example Pty Ltd','Address':'Road'}],config,source('b'),ONTOLOGY)
    links=resolve(a+b,tmp_path)
    assert len(links)==1
    assert links[0]['canonical_entity_key']=='abn:51824753556'
    assert links[0]['evidence']['rule']=='exact_checksum_valid_acn'
    assert links[0]['confidence']==.985
    c,_=extract([{'ABN':'83914571673','ACN':'004085616','Name':'Example Pty Ltd','Address':'Road'}],config,source('c'),ONTOLOGY)
    assert resolve(a+b+c,tmp_path)==[]
