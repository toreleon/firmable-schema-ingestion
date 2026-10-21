import copy
from ingest.core import extract,load_jsonl,jsonl
from ingest.identity import record_context,alias_eligible
from ingest.resolve import resolve
from ingest.review import apply_link_reviews,version_evaluation
from test_engine import CONFIG,ONTOLOGY,source


def build(sid,rows):
    cfg=copy.deepcopy(CONFIG)
    cfg['mappings'][1]['confidence']=.6  # Name is deliberately not a verified legal-name claim.
    cfg['mappings'].append({'source_field':'ACN','canonical_field':'entity.acn','transforms':['acn'],'confidence':.95})
    src=source(sid)
    obs,_=extract(rows,cfg,src,ONTOLOGY)
    ctx=[record_context(row,cfg,src,i) for i,row in enumerate(rows,1)]
    return obs,{(c['source_id'],c['source_record_id']):c for c in ctx}


def row(name,kind='Company',abn='51824753556',acn=''):
    return {'Name':name,'Licence Type DESC':kind,'ABN':abn,'ACN':acn,'Address':'1 Road'}


def test_unmapped_raw_person_name_blocks_employer_identifier_merge(tmp_path):
    a,ca=build('a',[row('Example Safety Pty Ltd')])
    b,cb=build('b',[row('Alex Nguyen','Individual')])
    assert not any(o['canonical_field']=='entity.legal_name' for o in a+b)
    assert resolve(a+b,tmp_path,contexts=ca|cb)==[]
    assert {r['reason'] for r in load_jsonl(tmp_path/'outputs/unlinked_queue.jsonl')}=={'person_company_subject_mismatch'}


def test_trustee_capacity_cannot_be_merged_with_own_capacity(tmp_path):
    a,ca=build('a',[row('Example Water Pty Ltd',acn='004085616')])
    b,cb=build('b',[row('The trustee for Example Unit Trust',acn='004085616')])
    assert not alias_eligible(next(iter(cb.values())))
    assert resolve(a+b,tmp_path,contexts=ca|cb)==[]
    assert all(r['reason']=='trustee_or_trust_capacity_mismatch' for r in load_jsonl(tmp_path/'outputs/unlinked_queue.jsonl'))


def test_similar_names_do_not_override_distinct_source_subjects(tmp_path):
    a,ca=build('a',[row('Example Civil Pty Ltd')])
    b,cb=build('b',[row('Example Civils Pty Ltd')])
    assert resolve(a+b,tmp_path,contexts=ca|cb)==[]


def test_many_employees_cannot_outvote_valid_company_pair(tmp_path):
    a,ca=build('a',[row('Example Safety Pty Ltd')])
    b,cb=build('b',[row('EXAMPLE SAFETY PTY. LTD.')]+[row(f'Employee {i}','Individual') for i in range(5)])
    links=resolve(a+b,tmp_path,contexts=ca|cb)
    assert len(links)==1
    assert links[0]['evidence']['name_check']=='exact_normalized_raw_subject_names'
    assert len(load_jsonl(tmp_path/'outputs/unlinked_queue.jsonl'))==5


def test_two_supported_subjects_cannot_be_unioned_into_one_profile(tmp_path):
    a,ca=build('a',[row('Example Safety Pty Ltd'),row('Example Water Pty Ltd')])
    b,cb=build('b',[row('Example Safety Pty Ltd'),row('Example Water Pty Ltd')])
    assert resolve(a+b,tmp_path,contexts=ca|cb)==[]
    assert {r['reason'] for r in load_jsonl(tmp_path/'outputs/unlinked_queue.jsonl')}=={'contradictory_subjects_share_identifier'}


def test_person_record_cannot_create_company_identifier_alias(tmp_path):
    a,ca=build('a',[row('Alex Nguyen','Individual',acn='004085616')])
    b,cb=build('b',[row('Example Pty Ltd')])
    c,cc=build('c',[row('Example Pty Ltd',abn='',acn='004085616')])
    assert resolve(a+b+c,tmp_path,contexts=ca|cb|cc)==[]


def test_valid_company_bridge_and_punctuation_variants_are_kept(tmp_path):
    a,ca=build('a',[row('EXAMPLE PTY LTD',acn='004085616')])
    b,cb=build('b',[row('Example Pty. Ltd.')])
    c,cc=build('c',[row('Example Proprietary Limited',abn='',acn='004085616')])
    links=resolve(a+b+c,tmp_path,contexts=ca|cb|cc)
    assert len(links)==2
    assert all(l['canonical_entity_key']=='abn:51824753556' for l in links)


def test_unknown_contact_or_buyer_names_are_not_subject_evidence():
    cfg=copy.deepcopy(CONFIG);src=source('a')
    record={'Agency name':'Wrong Company Pty Ltd','Contact Name':'Alex Nguyen','Supplier name':'Right Company Pty Ltd'}
    ctx=record_context(record,cfg,src,1)
    assert [n['value'] for n in ctx['names']]==['Right Company Pty Ltd']


def test_new_sample_excludes_old_pairs_even_when_link_ids_change(tmp_path):
    def link(label,a,b):
        return {'link_id':label,'matcher_version':'v2','canonical_entity_key':'abn:example',
            'source_a_record':{'source_id':a,'source_record_id':'1'},'source_b_record':{'source_id':b,'source_record_id':'1'},'evidence':{}}
    old=dict(link('old-v1','a','b'),correct=True,reviewer='AI',review_notes='Prior sample')
    jsonl(tmp_path/'outputs/link_audit.jsonl',[old])
    proposals=[link('new-v2-same-pair','a','b'),link('new-v2-unseen','c','d')]
    proposals[1]['canonical_entity_key']='abn:new-example'
    report=version_evaluation(tmp_path,proposals,proposals)
    assert report['development_regression']['supported_pairs_retained']==1
    assert report['fresh_sample_size']==1
    assert load_jsonl(tmp_path/'outputs/link_audit_v2.jsonl')[0]['link_id']=='new-v2-unseen'
    assert report['fresh_sample_precision'] is None


def test_rejection_quarantine_survives_matcher_version_change(tmp_path):
    old={'link_id':'old-v1','source_a_record':{'source_id':'a','source_record_id':'1'},
        'source_b_record':{'source_id':'b','source_record_id':'1'},'correct':False}
    new={'link_id':'new-v2','source_a_record':{'source_id':'a','source_record_id':'1'},
        'source_b_record':{'source_id':'c','source_record_id':'1'},'evidence':{},'canonical_entity_key':'company'}
    jsonl(tmp_path/'outputs/link_audit.jsonl',[old])
    active,_,report=apply_link_reviews(tmp_path,[new])
    assert not active
    assert report['historical_rejected_reviews']==1
    assert report['quarantined_records']==2
