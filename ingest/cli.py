from __future__ import annotations
import argparse
from collections import Counter
from pathlib import Path
import json
import sys
import yaml
from .core import (config_digest,digest,extract,inventory,jsonl,load_jsonl,now,parse_records,read,save)
from .discovery import discover,rank_pool
from .acquire import acquire
from .agent import onboard
from .model import Model,AnthropicModel,LocalModel,demo_budget
from .resolve import resolve,profiles
from .identity import record_context


def all_observations(root):
    return [row for path in sorted((Path(root)/'outputs/observations').glob('*.jsonl')) for row in load_jsonl(path)]


def require_approval(config):
    review = config.get('review',{})
    if review.get('status') != 'approved' or review.get('approved_config_sha256') != config_digest(config):
        raise ValueError('Mapping requires approval of its current exact hash; use --draft for explicitly labelled review output')


def replay(root, draft=False):
    root = Path(root)
    ontology = yaml.safe_load((root/'firmable_ontology.yaml').read_text())
    selections = read(root/'selected_sources.json')
    results = []
    contexts={}
    for selection in selections:
        sid = selection['dataset_id']
        config_path = root/'outputs/mappings'/f'{sid}.json'
        if not config_path.exists():
            raise ValueError(f'Mapping missing for {sid}; finish runtime onboarding first')
        config = read(config_path)
        if not draft:
            require_approval(config)
        source = read(root/'data/sources'/f'{sid}.json')
        raw_path = root/source['local_path']
        if not raw_path.exists() and source.get('snapshot_path'):
            import gzip
            raw_path.parent.mkdir(parents=True,exist_ok=True)
            raw_path.write_bytes(gzip.decompress((root/source['snapshot_path']).read_bytes()))
        raw = raw_path.read_bytes()
        if digest(raw) != source['resource_sha256']:
            raise ValueError(f'Raw snapshot changed for {sid}; re-onboard before replay')
        rows = parse_records(raw,config['parser'],selection['limit'])
        source_contexts=[record_context(row,config,source,i) for i,row in enumerate(rows,1)]
        jsonl(root/'outputs/identity_context'/f'{sid}.jsonl',source_contexts)
        contexts.update({(c['source_id'],c['source_record_id']):c for c in source_contexts})
        observations, errors = extract(rows,config,source,ontology)
        for obs in observations:
            obs['mapping_review_status'] = config.get('review',{}).get('status','pending')
        jsonl(root/'outputs/observations'/f'{sid}.jsonl',observations)
        jsonl(root/'outputs/extraction_errors'/f'{sid}.jsonl',errors)
        jsonl(root/'outputs/record_inventory'/f'{sid}.jsonl',inventory(rows,source,config,observations))
        results.append({'source_id':sid,'input_records':len(rows),'observations':len(observations),'field_errors':len(errors)})
    observations = all_observations(root)
    links = resolve(observations,root,contexts=contexts)
    from .review import apply_link_reviews,version_evaluation
    profile_links,review_abstentions,review_application=apply_link_reviews(root,links)
    unlinked = load_jsonl(root/'outputs/unlinked_queue.jsonl')
    unlinked.extend(review_abstentions)
    for path in sorted((root/'outputs/record_inventory').glob('*.jsonl')):
        for row in load_jsonl(path):
            if row['observation_count'] == 0:
                unlinked.append(dict(row,reason='no_fields_mapped_confidently_business_candidate_requires_review'))
    jsonl(root/'outputs/unlinked_queue.jsonl',unlinked)
    resolution_summary = read(root/'outputs/resolution_summary.json')
    resolution_summary['unlinked_records'] = len(unlinked)
    resolution_summary['unlinked_reasons'] = dict(Counter(r['reason'] for r in unlinked))
    resolution_summary['review_application']=review_application
    save(root/'outputs/resolution_summary.json',resolution_summary)
    output = profiles(observations,profile_links,root)
    audit=audit_report(root)
    evaluation=version_evaluation(root,links,profile_links)
    current_audit=audit_report(root,'v2')
    submission_audit=(audit_report(root,'submission') if (root/'outputs/link_audit_submission.jsonl').exists() else None)
    measured_precision=submission_audit['link']['precision'] if submission_audit else evaluation['fresh_sample_precision']
    resolution_summary.update(measured_precision=measured_precision,historical_review_precision=audit['link']['precision'],
        precision_status=('V2 human review and approval recorded; per-link binary verdicts were not supplied.'
            if evaluation['v2_release_approved'] else 'Matcher v2 requires a fresh review; historical v1 sample is a development regression set.'),
        version_evaluation=evaluation,
        audit_sample_size=evaluation['fresh_sample_size'],
        historical_audit_sample_size=audit['link']['sample_size'],
        reviewer_kinds=current_audit['link']['reviewer_kinds'],
        historical_reviewer_kinds=audit['link']['reviewer_kinds'])
    if submission_audit:
        resolution_summary.update(submission_audit=submission_audit['link'],
            audit_sample_size=submission_audit['link']['sample_size'],
            precision_status='Current matcher, 50-link submission audit; numerical precision requires all 50 explicit correctness labels.')
    save(root/'outputs/resolution_summary.json',resolution_summary)
    summary = {'sources':results,'total_observations':len(observations),'proposed_links':len(links),
               'profile_eligible_links':len(profile_links),'review_rejected_links':review_application['reviewed_false_links'],
               'profiles':len(output),'requested_profiles':50,'draft':draft,
               'profile_target_met':len(output)>=50,'measured_precision':measured_precision,
               'historical_review_sample_precision':audit['link']['precision'],
               'fresh_audit_sample_size':evaluation['fresh_sample_size'],
               'v2_release_approved':evaluation['v2_release_approved'],
               'fresh_sample_human_reviewed':evaluation['fresh_sample_human_reviewed'],
               'fresh_sample_human_approved':evaluation['fresh_sample_human_approved'],
               'historical_v1_link_human_audit_complete':audit['link']['independent_human_audit_complete'],
               'human_audit_status':('v2 reviewed and approved by user; binary correctness verdicts not supplied' if evaluation['v2_release_approved'] else 'v1 link sample human-reviewed; fresh v2 review pending'
                   if audit['link']['independent_human_audit_complete'] else 'independent human sign-off pending; reviewer attribution recorded')}
    if submission_audit:
        summary.update(submission_audit_size=submission_audit['link']['sample_size'],
            submission_audit_reviewed=submission_audit['link']['reviewed'],
            submission_audit_reviewer_kinds=submission_audit['link']['reviewer_kinds'],
            human_audit_status='Current 50-link audit records explicit verdicts with reviewer attribution; unresolved triage items and any AI reviews remain declared.')
    save(root/'outputs/replay_summary.json',summary)
    return summary


def cost_report(root):
    root = Path(root)
    import re
    recorded = {}
    for path in sorted((root/'outputs').rglob('*.json')):
        if not any(path.name.endswith(suffix) for suffix in ['.model.json','.provider_response.json','.usage.json']):
            continue
        receipt = read(path)
        usage = receipt.get('usage')
        if not usage:
            continue
        provider_id = receipt.get('provider_id') or receipt.get('id') or str(path)
        sid = next((part for part in path.parts if re.fullmatch(r'[0-9a-f-]{36}',part)),None)
        old = recorded.get(provider_id,{})
        recorded[provider_id] = {'source_id':sid or old.get('source_id'),
            'input_tokens':usage.get('prompt_tokens',usage.get('input_tokens',0)),
            'output_tokens':usage.get('completion_tokens',usage.get('output_tokens',0)),
            'wall_seconds':receipt.get('wall_seconds',old.get('wall_seconds',0)),
            'model':receipt.get('model') or (receipt.get('native_response') or {}).get('model') or old.get('model','unknown')}
    sources = {}
    total_input=sum(r['input_tokens'] for r in recorded.values())
    total_output=sum(r['output_tokens'] for r in recorded.values())
    for run in sorted((root/'outputs/runs').glob('*')):
        if not run.is_dir():
            continue
        receipts = [read(p) for p in sorted(run.glob('*.model.json'))]
        measured = [r for r in recorded.values() if r['source_id']==run.name]
        inp = sum(r['input_tokens'] for r in measured)
        out = sum(r['output_tokens'] for r in measured)
        state = read(run/'state.json') if (run/'state.json').exists() else {}
        final_receipts=[r for r in receipts if r.get('model')=='claude-haiku-5-5' and r.get('usage')]
        sources[run.name] = {'recorded_model_requests_all_attempts':len(measured),'input_tokens_all_attempts':inp,'output_tokens_all_attempts':out,
              'model_wall_seconds_all_attempts':sum(r.get('wall_seconds',0) for r in measured),
              'latest_step_receipts_input_tokens':sum(r['usage'].get('prompt_tokens',0) for r in final_receipts),
              'latest_step_receipts_output_tokens':sum(r['usage'].get('completion_tokens',0) for r in final_receipts),
              'latest_step_receipts_model_wall_seconds':sum(r.get('wall_seconds',0) for r in final_receipts),
              'final_invocation_wall_seconds':state.get('elapsed_seconds_this_invocation'),
              'status':state.get('status'),'step_count':state.get('step_count'),
              'api_usd_actual':None,'per_record_model_cost_usd':0,
              'models':sorted({r['model'] for r in receipts})}
    report = {'sources':sources,'input_tokens_recorded':total_input,'output_tokens_recorded':total_output,
              'actual_api_usd':None,'actual_api_usd_note':'Messages API does not return billed dollars; no billing export available. An initial truncated DeepSeek response and overwritten early QBCC attempts lack complete receipts: recorded tokens are a lower bound. Tiny API diagnostics also have incomplete accounting.',
              'recorded_requests':len(recorded),
              'per_record_model_cost_usd':0,'budget':read(root/'outputs/cost/budget.json') if (root/'outputs/cost/budget.json').exists() else None,
              'pricing_note':'Reservation uses conservative ceilings $10/M input and $20/M output, with one input token reserved per serialized request character. This is a spend guard, not a claimed invoice.'}
    confirmation_path=root/'outputs/user_confirmations.json'
    if confirmation_path.exists():
        billing=read(confirmation_path).get('billing',{})
        if billing.get('actual_usd') is not None:
            report.update(actual_api_usd=billing['actual_usd'],
                actual_api_usd_scope=billing.get('scope'),actual_api_provider=billing.get('provider'),
                actual_api_model_family=billing.get('model_family'),
                actual_api_usd_note='Actual Haiku spend checked and confirmed by the user in conversation; per-source dollars are not allocated. Recorded token totals remain lower bounds.',
                billing_confirmation='outputs/user_confirmations.json')
    demo_path=root/'outputs/cost/demo_budget.json'
    if demo_path.exists():
        report['demo_allowance']=read(demo_path)
    save(root/'outputs/cost_summary.json',report)
    return report


def audit_report(root,version='v1'):
    root = Path(root)
    results = {}
    for name,field,required in [('triage','contains_business_entities',20),('link','correct',50)]:
        path = root/'outputs'/('link_audit_submission.jsonl' if name=='link' and version=='submission'
            else 'link_audit_v2.jsonl' if name=='link' and version=='v2' else f'{name}_audit.jsonl')
        rows = load_jsonl(path)
        reviewed = [r for r in rows if type(r.get(field)) is bool and r.get('reviewer') and r.get('review_notes')]
        assessed=[r for r in rows if r.get('reviewer') and r.get('review_notes')]
        positive = sum(r[field] for r in reviewed)
        negatives=len(reviewed)-positive
        unresolved=len(rows)-len(reviewed)
        reviewer_kinds=dict(Counter(r.get('reviewer_kind','unspecified') for r in assessed))
        approvals=sum(r.get('approved') is True and r.get('reviewer_kind')=='human' for r in assessed)
        results[name] = {'sample_size':len(rows),'reviewed':len(reviewed),'positive':positive,
                         'negative':negatives,'inconclusive':unresolved,'assessed':len(assessed),
                         'all_items_assessed':len(assessed)==required,'reviewer_kinds':reviewer_kinds,
                         'human_approved':approvals,'binary_verdicts_recorded':len(reviewed),
                         'available_sample_human_review_complete':bool(rows) and len(assessed)==len(rows) and all(r.get('reviewer_kind')=='human' for r in assessed),
                         'independent_human_audit_complete':len(reviewed)==required and all(r.get('reviewer_kind')=='human' for r in reviewed),
                         'complete':len(reviewed)==required,
                         'precision':positive/len(reviewed) if reviewed else None,
                         'false_positive_rate':(len(reviewed)-positive)/len(reviewed) if reviewed and name=='triage' else None,
                         'scope':'Conclusive cases in frozen proposal sample; reviewer attribution is preserved. AI review is not independent human sign-off.'}
        if name=='triage':
            results[name].update(verification_coverage=len(reviewed)/len(rows) if rows else 0,
                full_sample_false_positive_rate=None if unresolved or not rows else negatives/len(rows),
                full_sample_false_positive_rate_bounds=[negatives/len(rows),(negatives+unresolved)/len(rows)] if rows else None)
        elif version=='submission':
            results[name].update(conclusive_precision=positive/len(reviewed) if reviewed else None,
                precision=positive/len(rows) if len(rows)==required and len(reviewed)==required else None,
                threshold=sorted({r.get('threshold') for r in rows if r.get('threshold') is not None}),
                sampling='Retained identical raw pairs from the earlier human audit plus seeded additional current proposals; development/convenience sample, not independent population precision.')
    output_name={'v1':'audit_summary.json','v2':'audit_summary_v2.json','submission':'audit_summary_submission.json'}[version]
    save(root/'outputs'/output_name,results)
    discovery_path=root/'outputs/discovery_summary.json'
    if discovery_path.exists():
        discovery=read(discovery_path)
        discovery.update(false_positive_rate=results['triage']['false_positive_rate'],
            false_positive_rate_denominator=results['triage']['reviewed'],
            inconclusive_sample_items=results['triage']['inconclusive'],
            full_sample_false_positive_rate=results['triage']['full_sample_false_positive_rate'],
            full_sample_false_positive_rate_bounds=results['triage']['full_sample_false_positive_rate_bounds'],
            audit_status='Reviewer-labelled sample; attribution and inconclusive cases retained in audit_summary.json')
        save(discovery_path,discovery)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reviewable agentic business ingestion')
    parser.add_argument('--root',default='.')
    sub = parser.add_subparsers(dest='command',required=True)
    sub.add_parser('discover')
    sub.add_parser('rank')
    sub.add_parser('acquire-all')
    p = sub.add_parser('onboard-all')
    p.add_argument('--provider',choices=['anthropic','deepseek','local'],default='anthropic')
    p.add_argument('--model',default=None)
    p.add_argument('--local-model-path')
    p.add_argument('--source-id',help='Resume one selected source')
    p.add_argument('--budget-scope',choices=['assignment','demo'],default='assignment')
    p.add_argument('--demo-request-limit',type=int,default=6)
    p.add_argument('--demo-usd-limit',type=float,default=1.0)
    p = sub.add_parser('onboard',help='Onboard a previously unseen raw source using a metadata JSON, not a hand-authored mapping')
    p.add_argument('--source',required=True)
    p.add_argument('--limit',type=int,default=1000)
    p.add_argument('--model',default=None)
    p.add_argument('--provider',choices=['anthropic','deepseek','local'],default='anthropic')
    p.add_argument('--local-model-path')
    p.add_argument('--budget-scope',choices=['assignment','demo'],default='assignment')
    p.add_argument('--demo-request-limit',type=int,default=6)
    p.add_argument('--demo-usd-limit',type=float,default=1.0)
    p = sub.add_parser('replay')
    p.add_argument('--draft',action='store_true')
    p = sub.add_parser('approve')
    p.add_argument('--source-id',required=True)
    p.add_argument('--reviewer',required=True)
    p.add_argument('--config-sha256',required=True)
    p.add_argument('--notes',required=True)
    p = sub.add_parser('reject')
    p.add_argument('--source-id',required=True)
    p.add_argument('--reviewer',required=True)
    p.add_argument('--notes',required=True)
    sub.add_parser('cost')
    p=sub.add_parser('audit')
    p.add_argument('--version',choices=['v1','v2','submission'],default='v1')
    sub.add_parser('evidence')
    p=sub.add_parser('import-audit')
    p.add_argument('--csv',required=True)
    p.add_argument('--version',choices=['v1','v2','submission'],default='v1')
    sub.add_parser('evidence-v2')
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    try:
        if args.command == 'discover':
            result = discover(root)
        elif args.command == 'rank':
            result = rank_pool(root)
        elif args.command == 'acquire-all':
            result = [acquire(root,**selection) for selection in read(root/'selected_sources.json')]
        elif args.command in {'onboard-all','onboard'}:
            provider = getattr(args,'provider','anthropic')
            if provider == 'local':
                if not args.local_model_path:
                    raise ValueError('--local-model-path is required')
                model = LocalModel(root,args.local_model_path)
            elif provider == 'anthropic':
                model = AnthropicModel(root,args.model or 'claude-haiku-5-5')
            else:
                model = Model(root,args.model)
            if args.budget_scope=='demo':
                model.budget_path=demo_budget(root,args.demo_request_limit,args.demo_usd_limit)
            if args.command == 'onboard':
                result = onboard(root,read(args.source),model,root/'firmable_ontology.yaml',args.limit)
            else:
                result = []
                for selection in read(root/'selected_sources.json'):
                    if args.source_id and selection['dataset_id'] != args.source_id:
                        continue
                    sid = selection['dataset_id']
                    print(f'Onboarding {sid}',flush=True)
                    result.append(onboard(root,read(root/'data/sources'/f'{sid}.json'),model,root/'firmable_ontology.yaml',selection['limit']))
        elif args.command == 'replay':
            result = replay(root,args.draft)
        elif args.command in {'approve','reject'}:
            path = root/'outputs/mappings'/f'{args.source_id}.json'
            config = read(path)
            fingerprint = config_digest(config)
            if args.command == 'approve' and fingerprint != args.config_sha256:
                raise ValueError(f'Config changed; current hash: {fingerprint}')
            config['review'] = {'status':'approved' if args.command == 'approve' else 'rejected',
                 'reviewer':args.reviewer,'notes':args.notes,'reviewed_at':now(),
                 'approved_config_sha256':fingerprint if args.command=='approve' else None}
            save(path,config)
            result = config['review']
        elif args.command == 'cost':
            result = cost_report(root)
        elif args.command == 'evidence':
            from .review import triage_evidence,link_evidence,export_audits
            result = dict(triage_evidence(root),**link_evidence(root),**export_audits(root))
        elif args.command == 'import-audit':
            from .review import import_audits
            result=import_audits(root,args.csv,{'v1':'link_audit.jsonl','v2':'link_audit_v2.jsonl','submission':'link_audit_submission.jsonl'}[args.version])
        elif args.command == 'evidence-v2':
            from .review import export_v2_review
            result=export_v2_review(root)
        else:
            result = audit_report(root,args.version)
        print(json.dumps(result,indent=2,default=str))
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr)
        return 1
    return 0
