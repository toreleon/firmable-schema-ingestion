"""Versioned, conservative identifier links. No transitive fuzzy-name merges."""
from collections import defaultdict, Counter
import random
from pathlib import Path
from .core import digest, jsonl, save, load_jsonl,valid_abn, valid_acn
from .identity import name_key,canonical_context,describe,check_pair,alias_eligible

MATCHER_VERSION = 'exact-identifiers-v2-subject-guards'


def value(observation):
    group, field = observation['canonical_field'].split('.')
    return observation[group][field]


def records_from(observations):
    records = {}
    for obs in observations:
        key = (obs['source_id'],obs['source_record_id'])
        row = records.setdefault(key, {'source_id':key[0], 'source_record_id':key[1], 'observations':[], 'fields':defaultdict(list)})
        row['observations'].append(obs)
        row['fields'][obs['canonical_field']].append(obs)
    return records


def resolve(observations, root, threshold=.98,contexts=None):
    root = Path(root)
    records = records_from(observations)
    groups = defaultdict(list)
    unlinked = []
    acn_abns = defaultdict(set)
    alias_evidence = defaultdict(list)
    for ref,row in records.items():
        scoped=any(o.get('provenance',{}).get('entity_scope') for o in row['observations'])
        row['context']=(describe([],[],scope='identifier_only_party') if scoped else
                        (contexts or {}).get(ref) or canonical_context(row))
        abns = {value(o) for o in row['fields'].get('entity.abn',[]) if valid_abn(value(o))}
        acns = {value(o) for o in row['fields'].get('entity.acn',[]) if valid_acn(value(o))}
        if len(abns)==1 and len(acns)==1 and alias_eligible(row['context']):
            acn=next(iter(acns));abn=next(iter(abns))
            acn_abns[acn].add(abn)
            alias_evidence[acn].append({'source_id':row['source_id'],'source_record_id':row['source_record_id'],
                'abn':abn,'acn':acn,'observation_ids':[o['observation_id'] for o in row['observations']
                if o['canonical_field'] in {'entity.abn','entity.acn'}]})

    def refuse(row, reason, **extra):
        unlinked.append({'source_id':row['source_id'],'source_record_id':row['source_record_id'], 'reason':reason,
                         'name_values':[n['value'] for n in row['context']['names']],
                         'subject_kind':row['context']['subject_kind'],'capacity':row['context']['capacity'],
                         'observation_ids':[o['observation_id'] for o in row['observations']], **extra})

    for row in records.values():
        abns = {value(o) for o in row['fields'].get('entity.abn',[]) if valid_abn(value(o))}
        acns = {value(o) for o in row['fields'].get('entity.acn',[]) if valid_acn(value(o))}
        if len(abns)>1 or len(acns)>1:
            refuse(row,'multiple_identifiers_in_one_record_no_safe_party_assignment')
            continue
        names=row['context']['name_keys']
        row['name'] = names[0] if len(names)==1 else ''
        row['acns'] = acns
        row['abns'] = abns
        if abns:
            groups['abn:'+next(iter(abns))].append(row)
        elif acns:
            acn=next(iter(acns))
            alias = acn_abns.get(acn,set())
            if len(alias)>1:
                refuse(row,'acn_cooccurs_with_conflicting_abns')
                continue
            compatible_alias=any(check_pair(row['context'],records[(e['source_id'],e['source_record_id'])]['context']) is None for e in alias_evidence[acn])
            groups['abn:'+next(iter(alias)) if len(alias)==1 and compatible_alias else 'acn:'+acn].append(row)
        else:
            refuse(row,'no_checksum_valid_identifier_name_alone_insufficient')

    links = []
    for key, members in sorted(groups.items()):
        if len({r['source_id'] for r in members}) < 2:
            for row in members:
                refuse(row,'identifier_not_observed_in_another_source',candidate_entity_key=key)
            continue
        distinct_acns = set().union(*(r['acns'] for r in members))
        if len(distinct_acns) > 1:
            for row in members:
                refuse(row,'same_abn_but_conflicting_explicit_acns',candidate_entity_key=key)
            continue
        members.sort(key=lambda r:(r['source_id'],r['source_record_id']))
        # Partition peers by exact normalized names before matching. Corporate suffixes
        # and punctuation can differ; a broad fuzzy majority cannot override a person.
        by_name=defaultdict(list)
        for member in members:
            for name in member['context']['name_keys']:
                by_name[name].append(member)
        emitted = set()
        for row in members:
            candidates={ (p['source_id'],p['source_record_id']):p
                for name in row['context']['name_keys'] for p in by_name[name] if p['source_id']!=row['source_id']}
            # Include a bounded diagnostic peer if names do not corroborate. It can
            # only produce a refusal; it cannot create a name-only or fuzzy match.
            if not candidates:
                peers=[p for p in members if p['source_id']!=row['source_id']]
                reasons=Counter(check_pair(row['context'],p['context']) or 'no_eligible_identifier_bridge' for p in peers[:20])
                priority=['contradictory_subject_type_evidence','person_company_subject_mismatch',
                    'trustee_or_trust_capacity_mismatch','incompatible_subject_types',
                    'compound_names_without_safe_party_assignment','missing_subject_name_corroboration','raw_subject_names_disagree']
                reason=next((r for r in priority if r in reasons),'no_compatible_cross_source_peer')
                refuse(row,reason,candidate_entity_key=key,blocked_reasons=dict(reasons))
                continue
            peer=None;bridges=[];failures=Counter()
            for ref,candidate in sorted(candidates.items()):
                reason=check_pair(row['context'],candidate['context'])
                if reason:
                    failures[reason]+=1
                    continue
                direct_abn=bool(row['abns'] & candidate['abns'])
                direct_acn=bool(row['acns'] & candidate['acns'])
                compatible_bridges=[e for acn in distinct_acns for e in alias_evidence[acn]
                    if check_pair(row['context'],records[(e['source_id'],e['source_record_id'])]['context']) is None
                    and check_pair(candidate['context'],records[(e['source_id'],e['source_record_id'])]['context']) is None]
                if not direct_abn and not direct_acn and not compatible_bridges:
                    failures['no_eligible_identifier_bridge']+=1
                    continue
                score=.995 if direct_abn else .985
                if score<threshold:
                    failures['candidate_below_link_threshold']+=1
                    continue
                peer=candidate;confidence=score;bridges=compatible_bridges
                break
            if peer is None:
                refuse(row,failures.most_common(1)[0][0] if failures else 'no_compatible_cross_source_peer',
                       candidate_entity_key=key,blocked_reasons=dict(failures))
                continue
            endpoints = sorted([(row['source_id'],row['source_record_id']), (peer['source_id'],peer['source_record_id'])])
            pair = tuple(endpoints)
            if pair in emitted:
                continue
            emitted.add(pair)
            left,right = (row,peer) if endpoints[0]==(row['source_id'],row['source_record_id']) else (peer,row)
            evidence = {'rule':'exact_checksum_valid_'+key.split(':')[0], 'identifier':key.split(':')[1],
                        'name_a':left['name'], 'name_b':right['name'],
                        'name_check':'exact_normalized_raw_subject_names','no_conflicting_explicit_acn':True,
                        'subject_context_a':left['context'],'subject_context_b':right['context'],
                        'subject_type_check':'compatible','capacity_check':'compatible',
                        'observation_ids_a':[o['observation_id'] for o in left['observations']],
                        'observation_ids_b':[o['observation_id'] for o in right['observations']]}
            if not (row['abns'] & peer['abns']) and (row['acns'] & peer['acns']):
                evidence['rule']='exact_checksum_valid_acn'
                evidence['identifier']=next(iter(row['acns'] & peer['acns']))
            elif not (row['abns'] & peer['abns']):
                evidence['rule']='explicit_unambiguous_acn_abn_cooccurrence'
                evidence['alias_evidence']=bridges
            links.append({'link_id':digest([MATCHER_VERSION,key,endpoints]),
                          'source_a_record':dict(source_id=endpoints[0][0],source_record_id=endpoints[0][1]),
                          'source_b_record':dict(source_id=endpoints[1][0],source_record_id=endpoints[1][1]),
                          'canonical_entity_key':key,'confidence':confidence,
                          'confidence_kind':'uncalibrated_rule_score','evidence':evidence,
                          'matcher_version':MATCHER_VERSION,'threshold':threshold,
                          'status':'proposed_pending_human_audit'})
    # Profiles union all links under one identifier key. Even individually valid
    # pairs must not silently merge two incompatible, independently supported subjects.
    by_key=defaultdict(list)
    for link in links:
        by_key[link['canonical_entity_key']].append(link)
    safe=[]
    for key,candidates in by_key.items():
        refs=sorted({(link[side]['source_id'],link[side]['source_record_id'])
                     for link in candidates for side in ('source_a_record','source_b_record')})
        if any(check_pair(records[a]['context'],records[b]['context'])
               for i,a in enumerate(refs) for b in refs[i+1:]):
            for ref in refs:
                refuse(records[ref],'contradictory_subjects_share_identifier',candidate_entity_key=key)
        else:
            safe.extend(candidates)
    links=safe
    jsonl(root/'outputs/proposed_links.jsonl',links)
    jsonl(root/'outputs/unlinked_queue.jsonl',unlinked)
    sample = random.Random(20261021).sample(links,min(50,len(links)))
    audit_path=root/'outputs/link_audit.jsonl'
    existing=load_jsonl(audit_path) if audit_path.exists() else []
    if not any(type(r.get('correct')) is bool or r.get('review_notes') for r in existing):
        jsonl(audit_path,[dict(link,correct=None,reviewer=None,review_notes=None) for link in sample])
    save(root/'outputs/resolution_summary.json', {'input_records':len(records),'proposed_links':len(links),
         'linked_entities':len({l['canonical_entity_key'] for l in links}),'unlinked_records':len(unlinked),
         'unlinked_reasons':dict(Counter(r['reason'] for r in unlinked)), 'threshold':threshold,
         'measured_precision':None,'precision_status':'pending independent human review',
         'audit_sample_size':len(sample),'audit_seed':20261021,'matcher_version':MATCHER_VERSION,
         'confidence_note':'Rule scores are not measured precision; do not claim 99.5% precision from the score.'})
    jsonl(root/'outputs/relationships.jsonl',[])
    return links


def canonical_value(field, val):
    return name_key(val) if field in {'entity.legal_name','entity.trading_name'} else str(val).casefold().strip()


def assemble(entity_key, record_keys, records, excluded_source=None):
    fields = defaultdict(list)
    for key in sorted(record_keys):
        if key[0] == excluded_source:
            continue
        for obs in records[key]['observations']:
            fields[obs['canonical_field']].append(obs)
    profile_fields = {}
    for field, claims in sorted(fields.items()):
        candidates = defaultdict(list)
        for claim in claims:
            candidates[canonical_value(field,value(claim))].append(claim)
        # Repeated records from one source do not count as independent corroboration.
        def rank(item):
            _, group = item
            best = max(group,key=lambda o:(o['confidence']['source_reliability'],o['confidence']['field_confidence'],o['observed_at'],o['observation_id']))
            return (best['confidence']['source_reliability'],len({o['source_id'] for o in group}),
                    best['confidence']['field_confidence'],best['observed_at'],best['observation_id'])
        ordered = sorted(candidates.items(),key=rank,reverse=True)
        winners = ordered[0][1]
        winner = max(winners,key=lambda o:(o['confidence']['source_reliability'],o['confidence']['field_confidence'],o['observed_at'],o['observation_id']))
        def provenance(group):
            return [{'source_id':o['source_id'],'source_record_id':o['source_record_id'],'observation_id':o['observation_id'],
                     'observed_at':o['observed_at'],'observed_at_basis':o['observed_at_basis'],
                     'confidence':o['confidence']} for o in group]
        conflict = len(ordered)>1
        # Keep confidence dimensions apart. Discount disputed field correctness, not link score.
        confidence = dict(winner['confidence'])
        if conflict:
            confidence['field_confidence'] = min(confidence['field_confidence'],.7)
        profile_fields[field] = {'value':value(winner),'confidence':confidence,
            'confidence_note':'Uncalibrated source/mapping estimates; conflicting field score capped at 0.7' if conflict else 'Uncalibrated source and mapping estimates',
            'provenance':provenance(winners), 'conflict':conflict,
            'selection_rule':'highest source reliability, then distinct-source support, mapping confidence, latest publication proxy; stable hash tie-break',
            'alternatives':[{'value':value(group[0]),'provenance':provenance(group),
                             'why_not_selected':'Lower lexicographic reconciliation rank; retained as a source claim'} for _,group in ordered[1:]]}
    return {'canonical_entity_key':entity_key,'fields':profile_fields,
            'profile_version':'field-reconciliation-v1','status':'draft_from_proposed_links',
            'sources':sorted({k[0] for k in record_keys if k[0] != excluded_source})}


def profiles(observations, links, root, limit=50):
    root = Path(root)
    records = records_from(observations)
    memberships = defaultdict(set)
    for link in links:
        for side in ('source_a_record','source_b_record'):
            ref = link[side]
            memberships[link['canonical_entity_key']].add((ref['source_id'],ref['source_record_id']))
    chosen = sorted(memberships,key=lambda k:(-len({r[0] for r in memberships[k]}),k))[:limit]
    output = [assemble(key,memberships[key],records) for key in chosen]
    for profile in output:
        supporting=[l for l in links if l['canonical_entity_key']==profile['canonical_entity_key']]
        approved=all(l.get('status')=='human_approved' for l in supporting)
        if approved:
            profile['status']='human_approved'
        profile['identity_evidence']={'matcher_versions':sorted({l['matcher_version'] for l in supporting}),
            'supporting_link_ids':sorted(l['link_id'] for l in supporting),
            'status':'human_approved' if approved else 'proposed_links_pending_independent_review'}
    jsonl(root/'outputs/company_profiles.jsonl',output)
    impacts = []
    for source in sorted({o['source_id'] for o in observations}):
        changed_values=lost_fields=provenance_changes=orphaned=0
        details=[]
        for before in output:
            key = before['canonical_entity_key']
            after = assemble(key,memberships[key],records,excluded_source=source)
            changed = [f for f,v in before['fields'].items() if f not in after['fields'] or after['fields'][f]['value'] != v['value']]
            lost = [f for f in before['fields'] if f not in after['fields']]
            if changed:
                changed_values += 1
                details.append({'canonical_entity_key':key,'changed_fields':changed,'lost_fields':lost})
            lost_fields += len(lost)
            if before['fields'] != after['fields']:
                provenance_changes += 1
            if len(after['sources']) < 2:
                orphaned += 1
        impacts.append({'removed_source':source,'profiles_with_value_changes':changed_values,'fields_lost':lost_fields,
                        'profiles_with_any_provenance_or_confidence_change':provenance_changes,
                        'profiles_losing_cross_source_support':orphaned,'details':details,
                        'scope':'Fixed entity membership counterfactual; production withdrawal also retracts links and recomputes membership'})
    save(root/'outputs/source_removal_impact.json',impacts)
    return output
