import csv
from ingest.core import jsonl,load_jsonl,read,save,config_digest,digest
from ingest.review import import_audits,apply_link_reviews,current_release_approval
from ingest.cli import audit_report,cost_report
from ingest.resolve import resolve
from test_engine import observations


def test_release_approval_preserves_unknown_precision_and_expires_on_change(tmp_path):
    config={'mappings':[]}
    save(tmp_path/'outputs/mappings/source.json',config)
    code=tmp_path/'ingest/matcher.py';code.parent.mkdir();code.write_text('version 1')
    approval={'approved':True,'reviewer':'User','profile_link_ids':['link'],
        'mapping_hashes':{'source':config_digest(config)},
        'code_sha256':{'ingest/matcher.py':digest(code.read_bytes())}}
    save(tmp_path/'outputs/user_confirmations.json',{'v2_review':approval})
    link={'link_id':'link','source_a_record':{'source_id':'a','source_record_id':'1'},
        'source_b_record':{'source_id':'b','source_record_id':'1'},'canonical_entity_key':'company','evidence':{}}
    active,_,_=apply_link_reviews(tmp_path,[link])
    assert active[0]['status']=='human_approved'
    assert 'audit_review' not in active[0]  # Approval cannot invent a correctness verdict.
    assert current_release_approval(tmp_path)==approval
    jsonl(tmp_path/'outputs/link_audit.jsonl',[dict(link,correct=False)])
    assert apply_link_reviews(tmp_path,[link])[0]==[]  # Historical rejection still wins.
    code.write_text('version 2')
    assert current_release_approval(tmp_path)=={}


def test_submission_precision_requires_all_fifty_binary_labels(tmp_path):
    jsonl(tmp_path/'outputs/triage_audit.jsonl',[])
    rows=[{'link_id':str(i),'correct':True if i<44 else None,
        'reviewer':'User' if i<44 else 'User approval only','reviewer_kind':'human',
        'review_notes':'Existing evidence' if i<44 else 'Approved, no verdict','threshold':.98} for i in range(50)]
    jsonl(tmp_path/'outputs/link_audit_submission.jsonl',rows)
    report=audit_report(tmp_path,'submission')['link']
    assert report['precision'] is None and report['reviewed']==44
    for row in rows[44:]:
        row.update(correct=True,reviewer='Delegated AI',reviewer_kind='ai',review_notes='Compared raw source evidence')
    jsonl(tmp_path/'outputs/link_audit_submission.jsonl',rows)
    report=audit_report(tmp_path,'submission')['link']
    assert report['precision']==1 and report['reviewed']==50
    assert report['reviewer_kinds']=={'human':44,'ai':6}
    assert report['threshold']==[.98]
    assert not report['independent_human_audit_complete']


def test_confirmed_actual_spend_survives_regenerating_cost_report(tmp_path):
    save(tmp_path/'outputs/cost/budget.json',{'reserved_usd':9})
    save(tmp_path/'outputs/user_confirmations.json',{'billing':{
        'actual_usd':8.0687,'provider':'anthropic','model_family':'Haiku',
        'scope':'Haiku usage confirmed by user'}})
    report=cost_report(tmp_path)
    assert report['actual_api_usd']==8.0687
    assert report['budget']['reserved_usd']==9
    assert report['actual_api_model_family']=='Haiku'
    assert report['sources']=={}  # Never invent a per-source allocation.
    assert read(tmp_path/'outputs/cost_summary.json')['actual_api_usd']==8.0687


def test_inconclusive_reviews_keep_notes_and_stay_out_of_denominator(tmp_path):
    jsonl(tmp_path/'outputs/triage_audit.jsonl',[{'dataset_id':'a'},{'dataset_id':'b'}])
    jsonl(tmp_path/'outputs/link_audit.jsonl',[{'link_id':'pair'}])
    path=tmp_path/'reviews.csv'
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['audit_type','item_id','verdict','reviewer','review_notes'])
        writer.writeheader()
        for group,key,verdict,note in [('triage','a','false','Aggregate only'),('triage','b','','Source unavailable'),('link','pair','true','Identifier evidence verified')]:
            writer.writerow(dict(audit_type=group,item_id=key,verdict=verdict,reviewer='Codex (AI reviewer; not a human audit)',review_notes=note))
    result=import_audits(tmp_path,path)
    assert result=={'imported_decisions':2,'imported_inconclusive_assessments':1}
    triage=load_jsonl(tmp_path/'outputs/triage_audit.jsonl')
    assert triage[1]['contains_business_entities'] is None
    assert triage[1]['review_notes']=='Source unavailable'
    report=audit_report(tmp_path)
    assert report['triage']['false_positive_rate']==1
    assert report['triage']['full_sample_false_positive_rate_bounds']==[.5,1]
    assert report['triage']['assessed']==2
    assert report['triage']['reviewer_kinds']=={'ai':2}
    assert not report['link']['independent_human_audit_complete']


def test_replay_preserves_labels_and_quarantines_incident_links(tmp_path):
    obs=observations('a')+observations('b')+observations('c')
    links=resolve(obs,tmp_path)
    rejected=links[0]
    original=load_jsonl(tmp_path/'outputs/link_audit.jsonl')
    for row in original:
        row.update(correct=row['link_id']!=rejected['link_id'],reviewer='AI reviewer',reviewer_kind='ai',review_notes='Frozen reviewed sample')
    jsonl(tmp_path/'outputs/link_audit.jsonl',original)
    regenerated=resolve(obs,tmp_path)
    assert load_jsonl(tmp_path/'outputs/link_audit.jsonl')==original
    active,abstentions,summary=apply_link_reviews(tmp_path,regenerated)
    assert summary['reviewed_false_links']==1
    assert summary['quarantined_records']==2
    assert not active  # Both star edges depend on a quarantined anchor.
    assert len(abstentions)==3
    assert load_jsonl(tmp_path/'outputs/link_audit.jsonl')==original
