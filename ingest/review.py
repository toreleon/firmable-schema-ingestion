"""Review evidence, with no automatic claims of having performed a human audit."""
import concurrent.futures
import csv
import json
from pathlib import Path
from urllib.parse import urlencode
from .core import config_digest,digest,fetch,jsonl,load_jsonl,parse_records,read,save


def current_release_approval(root):
    """A human release approval is separate from per-link correctness verdicts."""
    root=Path(root)
    path=root/'outputs/user_confirmations.json'
    approval=read(path).get('v2_review',{}) if path.exists() else {}
    if not approval.get('approved') or not approval.get('mapping_hashes') or not approval.get('code_sha256'):
        return {}
    for sid,expected in approval['mapping_hashes'].items():
        config=root/'outputs/mappings'/f'{sid}.json'
        if not config.exists() or config_digest(read(config))!=expected:
            return {}
    for name,expected in approval['code_sha256'].items():
        code=root/name
        if not code.exists() or digest(code.read_bytes())!=expected:
            return {}
    return approval


def triage_evidence(root):
    root=Path(root)
    pool={p['id']:p for p in load_jsonl(root/'data/catalogue_pool.jsonl')}
    audit=load_jsonl(root/'outputs/triage_audit.jsonl')
    def inspect(item):
        p=pool[item['dataset_id']]
        result={'dataset_id':p['id'],'title':p['title'],'metadata_notes':p.get('notes'),
                'licence':p.get('license_title'),'resources':[
                    {k:r.get(k) for k in ['id','name','format','url','datastore_active']} for r in p.get('resources',[])],
                'catalogue_url':p['_portal'].split('/api/')[0]+'/dataset/'+p['id'],
                'human_verdict':item.get('contains_business_entities') if item.get('reviewer_kind')=='human' else None,
                'review_verdict':item.get('contains_business_entities'),
                'reviewer':item.get('reviewer'),'reviewer_kind':item.get('reviewer_kind'),
                'review_notes':item.get('review_notes'),'review_status':item.get('review_status')}
        resource=next((r for r in p.get('resources',[]) if r.get('datastore_active')),None)
        if resource:
            url=p['_portal']+'datastore_search?'+urlencode({'resource_id':resource['id'],'limit':5})
            try:
                payload=json.loads(fetch(url,timeout=25)[0])
                result['sample_url']=url
                result['sample']=payload.get('result',{}).get('records',[])
                result['fields']=payload.get('result',{}).get('fields',[])
            except Exception as exc:result['sample_error']=str(exc)
        else:
            result['sample_status']='No DataStore sample: reviewer must inspect linked resource; metadata alone is not proof.'
        return result
    with concurrent.futures.ThreadPoolExecutor(4) as executor:
        result=list(executor.map(inspect,audit))
    jsonl(root/'outputs/review/triage_evidence.jsonl',result)
    return {'triage_evidence_items':len(result)}


def link_evidence(root,audit_name='link_audit.jsonl',output_name='link_evidence.jsonl'):
    root=Path(root)
    links=load_jsonl(root/'outputs'/audit_name)
    observations=[o for p in sorted((root/'outputs/observations').glob('*.jsonl')) for o in load_jsonl(p)]
    by_record={}
    for obs in observations:
        by_record.setdefault((obs['source_id'],obs['source_record_id']),[]).append(obs)
    selections={r['dataset_id']:r for r in read(root/'selected_sources.json')}
    rows_cache={}
    for sid in {l[side]['source_id'] for l in links for side in ['source_a_record','source_b_record']}:
        source=read(root/'data/sources'/f'{sid}.json')
        config=read(root/'outputs/mappings'/f'{sid}.json')
        rows_cache[sid]=parse_records((root/source['local_path']).read_bytes(),config['parser'],selections[sid]['limit'])
    results=[]
    for link in links:
        evidence={'link_id':link['link_id'],'canonical_entity_key':link['canonical_entity_key'],
                  'rule_evidence':link['evidence'],'correct':link.get('correct'),
                  'reviewer':link.get('reviewer'),'reviewer_kind':link.get('reviewer_kind'),
                  'review_notes':link.get('review_notes'),'approved':link.get('approved'),
                  'review_status':link.get('review_status')}
        for side in ['source_a_record','source_b_record']:
            ref=link[side];obs=by_record[(ref['source_id'],ref['source_record_id'])]
            row_number=obs[0]['provenance']['row_number']
            evidence[side]={'reference':ref,'raw_row':rows_cache[ref['source_id']][row_number-1],
                            'observations':obs,'source_metadata':read(root/'data/sources'/f"{ref['source_id']}.json")}
        results.append(evidence)
    jsonl(root/'outputs/review'/output_name,results)
    return {'link_evidence_items':len(results)}


def export_audits(root):
    root=Path(root)
    rows=[]
    for item in load_jsonl(root/'outputs/review/triage_evidence.jsonl'):
        rows.append({'audit_type':'triage','item_id':item['dataset_id'],'title_or_entity':item['title'],
            'evidence_a':json.dumps({'url':item['catalogue_url'],'fields':item.get('fields'),'records':item.get('sample'),
                                   'unavailable':item.get('sample_status')},ensure_ascii=False),
            'evidence_b':'','verdict':'','reviewer':'','review_notes':''})
    for item in load_jsonl(root/'outputs/review/link_evidence.jsonl'):
        rows.append({'audit_type':'link','item_id':item['link_id'],'title_or_entity':item['canonical_entity_key'],
            'evidence_a':json.dumps(item['source_a_record']['raw_row'],ensure_ascii=False),
            'evidence_b':json.dumps(item['source_b_record']['raw_row'],ensure_ascii=False),
            'verdict':'','reviewer':'','review_notes':''})
    path=root/'outputs/review/audit_results.csv'
    # Never overwrite a reviewer's existing completed work.
    if path.exists():
        with path.open(newline='') as f:
            if any(row.get('verdict') for row in csv.DictReader(f)):
                raise ValueError('Existing CSV contains audit decisions; import/save them before regenerating')
    with path.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['audit_type','item_id','title_or_entity','evidence_a','evidence_b','verdict','reviewer','review_notes'])
        writer.writeheader();writer.writerows(rows)
    return {'review_csv_rows':len(rows),'review_csv':str(path)}


def import_audits(root,path,link_audit_name='link_audit.jsonl'):
    root=Path(root)
    datasets=load_jsonl(root/'outputs/triage_audit.jsonl')
    links=load_jsonl(root/'outputs'/link_audit_name)
    index={'triage':{r['dataset_id']:r for r in datasets},'link':{r['link_id']:r for r in links}}
    imported=0
    unresolved=0
    seen=set()
    with Path(path).open(newline='') as f:
        for row in csv.DictReader(f):
            verdict=row.get('verdict','').strip().lower()
            group=row['audit_type']
            if group not in index or row['item_id'] not in index[group]:
                raise ValueError('Audit ID is outside the fixed sample')
            identity=(group,row['item_id'])
            if identity in seen:
                raise ValueError('Duplicate audit ID')
            seen.add(identity)
            if verdict not in {'','true','false'}:
                raise ValueError('Verdict must be true, false, or blank when inconclusive')
            if not verdict and not row.get('review_notes') and not row.get('reviewer'):
                continue
            if not row.get('reviewer') or not row.get('review_notes'):
                raise ValueError('Every assessed row needs reviewer and notes, including inconclusive rows')
            target=index[group][row['item_id']]
            target['correct' if group=='link' else 'contains_business_entities']=(verdict=='true') if verdict else None
            target['reviewer']=row['reviewer'];target['review_notes']=row['review_notes']
            kind=row.get('reviewer_kind','').strip().lower()
            if 'ai reviewer' in row['reviewer'].casefold():
                if kind=='human':
                    raise ValueError('Reviewer attribution contradicts declared human review')
                kind='ai'
            target['reviewer_kind']=kind or 'unspecified'
            target['review_status']='conclusive' if verdict else 'inconclusive'
            imported+=bool(verdict)
            unresolved+=not bool(verdict)
    jsonl(root/'outputs/triage_audit.jsonl',datasets)
    jsonl(root/'outputs'/link_audit_name,links)
    return {'imported_decisions':imported,'imported_inconclusive_assessments':unresolved}


def apply_link_reviews(root,links):
    """Conservative review overlay; retain the evaluated proposals and frozen sample."""
    root=Path(root)
    approval=current_release_approval(root)
    approved_ids=set(approval.get('profile_link_ids',[]))
    audit_path=root/'outputs/link_audit.jsonl'
    reviews={r['link_id']:r for r in load_jsonl(audit_path)} if audit_path.exists() else {}
    v2_path=root/'outputs/link_audit_v2.jsonl'
    if v2_path.exists():
        reviews.update({r['link_id']:r for r in load_jsonl(v2_path) if type(r.get('correct')) is bool})
    submission_path=root/'outputs/link_audit_submission.jsonl'
    if submission_path.exists():
        reviews.update({r['link_id']:r for r in load_jsonl(submission_path) if type(r.get('correct')) is bool})
    rejected={link['link_id']:reviews[link['link_id']] for link in links
              if link['link_id'] in reviews and reviews[link['link_id']].get('correct') is False}
    quarantine={}
    historical_rejections=[r for r in reviews.values() if r.get('correct') is False]
    for link in historical_rejections:
        if 'source_a_record' in link and 'source_b_record' in link:
            for side in ['source_a_record','source_b_record']:
                ref=link[side];key=(ref['source_id'],ref['source_record_id'])
                quarantine.setdefault(key,[]).append(link['link_id'])
    active=[]
    withheld=[]
    for link in links:
        review=reviews.get(link['link_id'])
        if review and type(review.get('correct')) is bool:
            link['audit_review']={key:review.get(key) for key in ['correct','reviewer','reviewer_kind','review_notes']}
        dependencies=[link['source_a_record'],link['source_b_record']]+link['evidence'].get('alias_evidence',[])
        affected=sorted({bad for ref in dependencies for bad in quarantine.get((ref['source_id'],ref['source_record_id']),[])})
        if affected:
            link['status']='rejected_by_review' if link['link_id'] in rejected else 'withheld_due_to_review_quarantine'
            link['review_quarantine_causes']=affected
            withheld.append(link)
        else:
            if link['link_id'] in approved_ids:
                link['status']='human_approved'
                link['release_approval']={'reviewer':approval['reviewer'],
                    'confirmation':'outputs/user_confirmations.json',
                    'note':'Human approval; a binary correctness verdict is recorded separately.'}
            else:
                link['status']='review_supported_pending_human_approval' if review and review.get('correct') is True else 'proposed_pending_human_audit'
            active.append(link)
    all_refs={(l[s]['source_id'],l[s]['source_record_id']) for l in links for s in ['source_a_record','source_b_record']}
    active_refs={(l[s]['source_id'],l[s]['source_record_id']) for l in active for s in ['source_a_record','source_b_record']}
    abstentions=[{'source_id':sid,'source_record_id':rid,
        'reason':'record_quarantined_by_rejected_link_review' if (sid,rid) in quarantine else 'no_remaining_link_after_review_quarantine',
        'rejected_link_ids':quarantine.get((sid,rid),[])} for sid,rid in sorted(all_refs-active_refs)]
    report={'policy':'Quarantine both endpoints of rejected links and any explicit alias evidence that depends on them; no claim about which source is wrong.',
        'evaluated_proposals':len(links),'evaluated_population_sha256':digest(sorted(l['link_id'] for l in links)),
        'reviewed_false_links':len(rejected),'quarantined_records':len(quarantine),
        'historical_rejected_reviews':len(historical_rejections),
        'withheld_links':len(withheld),'profile_eligible_links':len(active),
        'profile_eligible_entities':len({l['canonical_entity_key'] for l in active}),
        'precision_note':'The review result belongs to the original frozen proposal sample. No precision claim is made for the filtered remainder.'}
    jsonl(root/'outputs/proposed_links.jsonl',links)
    jsonl(root/'outputs/profile_links.jsonl',active)
    jsonl(root/'outputs/rejected_or_withheld_links.jsonl',withheld)
    save(root/'outputs/review_application.json',report)
    return active,abstentions,report


def pair_key(link):
    """Version-independent raw-record pair, unlike a versioned link_id."""
    return tuple(sorted((link[side]['source_id'],link[side]['source_record_id']) for side in ['source_a_record','source_b_record']))


def version_evaluation(root,proposals,profile_links):
    import random
    root=Path(root)
    old=load_jsonl(root/'outputs/link_audit.jsonl')
    judged=[r for r in old if type(r.get('correct')) is bool]
    proposal_pairs={pair_key(r) for r in proposals}
    active_pairs={pair_key(r) for r in profile_links}
    old_pairs={pair_key(r) for r in old}
    old_refs={ref for pair in old_pairs for ref in pair}
    old_entities={r['canonical_entity_key'] for r in old}
    for link in old:
        evidence=link.get('evidence',{})
        for kind in ['abn','acn']:
            if evidence.get('rule')=='exact_checksum_valid_'+kind and evidence.get('identifier'):
                old_entities.add(kind+':'+evidence['identifier'])
        for bridge in evidence.get('alias_evidence',[]):
            for kind in ['abn','acn']:
                if bridge.get(kind):old_entities.add(kind+':'+bridge[kind])
    regression={'previously_supported':sum(r['correct'] for r in judged),
        'previously_rejected':sum(not r['correct'] for r in judged),
        'supported_pairs_retained':sum(r['correct'] and pair_key(r) in proposal_pairs for r in judged),
        'rejected_pairs_still_proposed':sum(not r['correct'] and pair_key(r) in proposal_pairs for r in judged),
        'supported_pairs_profile_eligible':sum(r['correct'] and pair_key(r) in active_pairs for r in judged),
        'scope':'Development regression only; v2 rules were informed by these v1 cases, so this is not new measured precision.'}
    unseen=sorted((l for l in profile_links if not (set(pair_key(l))&old_refs)
                   and l['canonical_entity_key'] not in old_entities),key=lambda l:l['link_id'])
    matcher=proposals[0]['matcher_version'] if proposals else 'exact-identifiers-v2-subject-guards'
    path=root/'outputs/link_audit_v2.jsonl'
    existing=load_jsonl(path) if path.exists() else []
    labelled=any(type(r.get('correct')) is bool or r.get('review_notes') for r in existing)
    if not labelled:
        selected=random.Random(matcher+':20261021').sample(unseen,min(50,len(unseen)))
        existing=[dict(l,correct=None,reviewer=None,reviewer_kind=None,review_notes=None,
            audit_scope='New v2 sample; excludes all v1-reviewed raw records and known entity identifier keys') for l in selected]
        jsonl(path,existing)
    reviewed=[r for r in existing if type(r.get('correct')) is bool and r.get('reviewer') and r.get('review_notes')]
    assessed=[r for r in existing if r.get('reviewer') and r.get('review_notes')]
    approved=[r for r in assessed if r.get('approved') and r.get('reviewer_kind')=='human']
    report={'matcher_version':matcher,'current_proposals':len(proposals),'profile_eligible_links':len(profile_links),
        'historical_sample_size':len(old),'development_regression':regression,
        'fresh_review_pool_size':len(unseen),'fresh_sample_size':len(existing),'fresh_sample_reviewed':len(reviewed),
        'fresh_sample_precision':sum(r['correct'] for r in reviewed)/len(reviewed) if reviewed else None,
        'fresh_review_attribution':sorted({r.get('reviewer_kind','unspecified') for r in reviewed}),
        'fresh_sample_human_reviewed':sum(r.get('reviewer_kind')=='human' for r in assessed),
        'fresh_sample_human_approved':len(approved),
        'fresh_sample_binary_verdicts_recorded':len(reviewed),
        'v2_release_approved':bool(current_release_approval(root)),
        'fresh_sample_path':'outputs/link_audit_v2.jsonl',
        'seed':matcher+':20261021','fresh_sample_disjoint_from_v1':all(pair_key(r) not in old_pairs for r in existing),
        'fresh_sample_record_disjoint_from_v1':all(not (set(pair_key(r))&old_refs) for r in existing),
        'fresh_sample_known_entity_keys_disjoint_from_v1':all(r['canonical_entity_key'] not in old_entities for r in existing),
        'fresh_sample_unique_entities':len({r['canonical_entity_key'] for r in existing}),
        'sample_target':50,'sample_shortfall':max(0,50-len(existing)),
        'precision_claim':'Human review/approval and binary correctness verdicts are recorded separately; no new precision is claimed without explicit true/false verdicts.'}
    save(root/'outputs/matcher_comparison.json',report)
    baseline=root/'outputs/experiments/matcher_v1_baseline/proposed_links.jsonl'
    if baseline.exists():
        previous=load_jsonl(baseline)
        current={pair_key(l):l for l in proposals}
        old_pairs_all={pair_key(l) for l in previous}
        changes=[]
        for old_link in previous:
            new_link=current.get(pair_key(old_link))
            changes.append({'previous_link_id':old_link['link_id'],
                'source_a_record':old_link['source_a_record'],'source_b_record':old_link['source_b_record'],
                'previous_entity_key':old_link['canonical_entity_key'],
                'current_link_id':new_link['link_id'] if new_link else None,
                'current_entity_key':new_link['canonical_entity_key'] if new_link else None,
                'outcome':'retained' if new_link else 'not_proposed_by_v2'})
        save(root/'outputs/link_migration_v1_to_v2.json',{'previous_matcher':'exact-identifiers-v1','current_matcher':matcher,
            'previous_proposals':len(previous),'current_proposals':len(proposals),
            'retained_pairs':sum(r['outcome']=='retained' for r in changes),
            'removed_pairs':sum(r['outcome']!='retained' for r in changes),
            'new_pairs':sum(pair_key(l) not in old_pairs_all for l in proposals),
            'canonical_key_changes':[r for r in changes if r['current_entity_key'] and r['previous_entity_key']!=r['current_entity_key']],
            'changes':changes,'scope':'Versioned proposals and key transitions; old evidence is retained, not overwritten.'})
    return report


def export_v2_review(root):
    root=Path(root)
    result=link_evidence(root,'link_audit_v2.jsonl','link_evidence_v2.jsonl')
    output=root/'outputs/review/link_audit_v2.csv'
    if output.exists():
        with output.open(newline='') as f:
            if any(r.get('verdict') or r.get('reviewer') or r.get('review_notes') for r in csv.DictReader(f)):
                return dict(result,csv_status='Existing decisions preserved')
    rows=load_jsonl(root/'outputs/review/link_evidence_v2.jsonl')
    with output.open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['audit_type','item_id','title_or_entity','evidence_a','evidence_b','verdict','reviewer','reviewer_kind','review_notes'])
        writer.writeheader()
        for r in rows:
            writer.writerow({'audit_type':'link','item_id':r['link_id'],'title_or_entity':r['canonical_entity_key'],
                'evidence_a':json.dumps(r['source_a_record']['raw_row'],ensure_ascii=False),
                'evidence_b':json.dumps(r['source_b_record']['raw_row'],ensure_ascii=False),
                'verdict':'','reviewer':'','reviewer_kind':'','review_notes':''})
    return dict(result,review_csv=str(output))
