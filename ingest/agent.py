from __future__ import annotations
import collections
import json
import time
from pathlib import Path
import yaml
from .core import (digest, extract, fields_of, getpath, inspect_resource, inventory, jsonl,
                   now, parse_records, read, save, validate_config, ontology_fields,valid_abn,valid_acn)


def profile(rows):
    fields = sorted(set().union(*(set(fields_of(r)) for r in rows)))
    def identifier_counts(field):
        import re
        strings = [str(getpath(r,field)).strip() for r in rows if getpath(r,field) not in (None,'')]
        plain = [re.sub(r'[\s.-]','',s) for s in strings]
        labelled = {'ABN':[],'ACN':[]}
        for text in strings:
            for match in re.finditer(r'(?i)\b(ABN|ACN)[ :#]*([0-9][0-9 .-]{7,16}[0-9])',text):
                labelled[match[1].upper()].append(re.sub(r'[^0-9]','',match[2]))
        return {'checksum_valid_abn':sum(valid_abn(s) for s in plain),
                'checksum_valid_acn':sum(valid_acn(s) for s in plain),
                'raw_check_scope':'The two raw counts require numeric-only cells. A zero does NOT invalidate prefixed/labelled identifiers.',
                'labelled_abn_matches':len(labelled['ABN']),
                'labelled_abn_checksum_valid':sum(valid_abn(s) for s in labelled['ABN']),
                'labelled_acn_matches':len(labelled['ACN']),
                'labelled_acn_checksum_valid':sum(valid_acn(s) for s in labelled['ACN'])}
    return {'record_count':len(rows), 'fields':{f:{
        'nonempty':sum(getpath(r,f) not in (None,'') for r in rows),
        'types':dict(collections.Counter(type(getpath(r,f)).__name__ for r in rows)),
        'examples':list(dict.fromkeys(str(getpath(r,f))[:300] for r in rows if getpath(r,f) not in (None,'')))[:3]
        , 'identifier_checks':identifier_counts(f)
        } for f in fields}, 'sample':rows[:4]}


MAPPING_INSTRUCTION = '''Propose a conservative mapping config. Return JSON with:
parser (copy the parser from context), observed_at_field (only a true source statement/publication time or null),
mappings: [{source_field,canonical_field,transforms,confidence,rationale}],
unmapped_fields: [{source_field,reason}], minimum_field_confidence:0.8,
warnings:[...], entity_role (e.g. supplier, grant_recipient, licensee).
canonical_field must be one of the exact entity.* or address.* paths supplied.
Transforms are a JSON array of EXACT strings: "strip", "upper", "lower", "digits", "abn", "acn", "postcode",
or {op:date,format:"%d/%m/%Y"}, {op:enum,map:{raw:canonical},default:null},
or {op:split,delimiter:";"}. No expressions, code, constants, lookups or invented values.
For labelled free text, {op:regex,pattern:"...",group:1} extracts EXACTLY ONE match,
otherwise rejects the field. Patterns have a 50ms timeout. Never take the first of multiple businesses.
For multiple EXPLICIT ABNs in a cell, you may emit identifier-only business claims:
use {op:split,delimiter:";"} followed by "abn", OR {op:regex,pattern:"...",group:1,all:true} followed by "abn".
Set separate_entities:true on that IDENTIFIER mapping. The engine gives each identifier its own
party-scoped record ID, assigns NO other fields to those parties, and preserves the parent raw record.
This is safe for an explicitly labelled list of supplier ABNs; it does not pair names/addresses by index.
Do not leave all explicit ABNs unmapped merely because the record lists multiple suppliers.
Compound names/addresses without safe party attribution must remain unmapped.
abn/acn transforms validate checksums. Use postcode for numeric spreadsheets.
Map semantic meaning, not merely similar headings. A supplier name can be legal_name only
if source context reasonably establishes an organisation/business name; flag that assumption.
No guesses for entity_type or status: use explicit enum maps only when evidence supports them.
Do not map contract value, buyer, project location, or award date into business identity/address/registration.
List every remaining source field as unmapped, including low-confidence candidates.
Do not infer ACN from ABN. No reconciliation at ingest. Multiple trading names use split.
Confidence is mapping correctness only, not publisher reliability or entity link confidence.'''


def mapping_schema(ontology):
    transform = {'anyOf':[
        {'type':'string','enum':['strip','upper','lower','digits','abn','acn','postcode']},
        {'type':'object','properties':{'op':{'type':'string','enum':['date','enum','split','regex']},
            'format':{'type':'string'},'map':{'type':'object'},'default':{},'delimiter':{'type':'string'},
            'pattern':{'type':'string'},'all':{'type':'boolean'},'group':{'anyOf':[{'type':'integer'},{'type':'string'}]}},'required':['op'],'additionalProperties':False}]}
    return {'type':'object','properties':{
        'parser':{'type':'object'},'observed_at_field':{'type':['string','null']},
        'mappings':{'type':'array','items':{'type':'object','properties':{
            'source_field':{'type':'string'},'canonical_field':{'type':'string','enum':list(ontology_fields(ontology))},
            'transforms':{'type':'array','items':transform},'confidence':{'type':'number','minimum':0,'maximum':1},
            'separate_entities':{'type':'boolean'},
            'rationale':{'type':'string'}},'required':['source_field','canonical_field','transforms','confidence','rationale'],'additionalProperties':False}},
        'unmapped_fields':{'type':'array','items':{'type':'object','properties':{'source_field':{'type':'string'},'reason':{'type':'string'}},'required':['source_field','reason'],'additionalProperties':False}},
        'minimum_field_confidence':{'type':'number','const':.8},'warnings':{'type':'array','items':{'type':'string'}},
        'entity_role':{'type':'string'}},
        'required':['parser','observed_at_field','mappings','unmapped_fields','minimum_field_confidence','warnings','entity_role'],
        'additionalProperties':False}


def unwrap_config(config):
    # Some providers serialize a nested tool argument as JSON text. This repairs
    # representation only; it never selects fields, changes confidence, or authors mappings.
    for _ in range(5):
        if isinstance(config,str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError as exc:
                # Preserve a complete mapping object when a provider incorrectly appends
                # sibling review metadata inside its string argument. Never repair field values.
                first,end = json.JSONDecoder().raw_decode(config)
                tail = config[end:].lstrip()
                if not tail.startswith((',"changes"', ',"semantic_risks"', ',"requires_human_review"')):
                    raise exc
                config = first
        elif isinstance(config,dict) and 'mappings' not in config and 'config' in config:
            config = config['config']
        else:
            break
    if not isinstance(config,dict) or not isinstance(config.get('mappings'),list):
        raise ValueError('Model did not return a mapping object')
    return config


def validation(rows, indices, config, source, ontology):
    field_reports = []
    for mapping in config.get('mappings',[]):
        try:
            isolated = dict(config,mappings=[mapping],minimum_field_confidence=0)
            obs,errs = extract(rows,isolated,source,ontology,row_numbers=indices,diagnostic=True)
            field_reports.append({'source_field':mapping['source_field'],'canonical_field':mapping['canonical_field'],
                                  'valid_emissions':len(obs),'errors':len(errs),'error_examples':errs[:2]})
        except Exception as exc:
            field_reports.append({'source_field':mapping.get('source_field'),'error':str(exc)})
    try:
        observations, errors = extract(rows, config, source, ontology, row_numbers=indices)
    except Exception as exc:
        return {'fatal_error':str(exc), 'records':len(rows), 'observation_count':0, 'error_count':0,'per_field_validation':field_reports}
    counts = collections.Counter(o['canonical_field'] for o in observations)
    return {'records':len(rows), 'observation_count':len(observations), 'emitted_by_field':dict(counts),
            'error_count':len(errors), 'errors':errors[:20],'per_field_validation':field_reports,
            'error_reasons':dict(collections.Counter(e['reason'] for e in errors)),
            'example_observations':observations[:8],
            'note':'Type/checksum validation does not prove semantic correctness; semantic review required.'}


def onboard(root, source, model, ontology_path, limit=1000):
    root = Path(root)
    run = root/'outputs/runs'/source['source_id']
    run.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    ontology = yaml.safe_load(Path(ontology_path).read_text())
    raw = (root/source['local_path']).read_bytes()
    identity = digest({'raw':digest(raw),'ontology':digest(ontology),'limit':limit,'engine':'0.1.0'})
    state_path = run/'state.json'
    state = read(state_path) if state_path.exists() else {'source_id':source['source_id'], 'started_at':now(), 'steps':[]}
    if state.get('identity') not in (None,identity):
        raise RuntimeError('Source/ontology changed; use a new run identifier to preserve previous receipts')
    state['identity'] = identity

    def checkpoint(step, payload):
        save(run/f'{step}.json',payload)
        if step not in state['steps']:
            state['steps'].append(step)
        state['status'] = step
        state['last_checkpoint_at'] = now()
        save(state_path,state)

    try:
        inspection = inspect_resource(raw)
        # Parser inspection uses only the document head. Holdout records are the final 30%.
        if 'head' in inspection:
            inspection['head'] = inspection['head'][:4000]
        checkpoint('01_inspect',inspection)
        guess = inspection['parser_guess'].copy()
        if guess['format'] == 'json':
            document = json.loads(raw)
            candidates = []
            def arrays(node,path='',depth=0):
                if isinstance(node,list) and node and all(isinstance(v,dict) for v in node):
                    candidates.append((len(node),path))
                elif isinstance(node,dict) and depth < 5:
                    for k,v in node.items():
                        arrays(v,f'{path}.{k}' if path else k,depth+1)
            arrays(document)
            if candidates:
                guess['record_path'] = max(candidates)[1]
        if guess['format'] in {'json','jsonl','csv'}:
            parser_reply = {'parser':guess,'warnings':['Deterministic format/container inspection; semantics are inferred by the model in proposal and revision.'],
                            'selected_by':'generic_format_and_container_inspection'}
        else:
            parser_reply = model.call(run,'02_parser',
                'Inspect the resource and choose parser settings. Return {parser:{...},warnings:[...]}. '
                'Supported: csv(format,delimiter,header_row,encoding); xlsx(format,sheet name or index,header_row); '
                'json/geojson(format,record_path dotted path to array); jsonl; xml(format,record_tag); '
                'zip(format,member,inner parser). Select a sheet with business entities. Do not invent columns.',
                {'source':source,'inspection':inspection})
        parser = parser_reply['parser']
        checkpoint('02_parser',parser_reply)
        try:
            rows = parse_records(raw,parser,limit)
        except (ValueError, KeyError, UnicodeError) as exc:
            checkpoint('02_parser_failure',{'parser':parser,'error':str(exc)})
            repaired = model.call(run,'02b_parser_revision',
                'The proposed parser failed. Correct it using the actual execution error. Return {parser:{...},warnings:[...]}. '
                'Do not change the resource. Use the observed parser_guess unless strong evidence contradicts it.',
                {'previous':parser_reply,'execution_error':str(exc),'inspection':inspection})
            parser = repaired['parser']
            rows = parse_records(raw,parser,limit)
            checkpoint('02b_parser_revision',repaired)
        if len(rows) < 10:
            raise ValueError('Fewer than 10 records; meaningful train/holdout split unavailable')
        cutoff = max(1, int(len(rows)*.7))
        train, holdout = rows[:cutoff], rows[cutoff:]
        # If inspection text reaches the nominal holdout, reject rather than claim unseen validation.
        if parser['format'] == 'csv' and len(raw) < 6000:
            raise ValueError('Small CSV could leak holdout through document-head inspection; reduce inspection size')
        checkpoint('03_profile', {'training':profile(train), 'holdout_count':len(holdout),
                                 'split':'first 70% training, final 30% holdout; document-head inspection only',
                                 'training_rows':[1,cutoff], 'holdout_rows':[cutoff+1,len(rows)]})
        context = {'ontology':ontology, 'allowed_canonical_fields':list(ontology_fields(ontology)),
                   'parser':parser,'source':source,'training':profile(train)}
        schema = mapping_schema(ontology)
        config = model.call(run,'04_proposal',MAPPING_INSTRUCTION,context,schema=schema)
        config = unwrap_config(config)
        config['parser'] = parser
        checkpoint('04_proposal',config)
        report = validation(holdout,list(range(cutoff+1,len(rows)+1)),config,source,ontology)
        checkpoint('05_holdout_validation',report)
        # A distinct model pass critiques the proposal using withheld execution results.
        revised = model.call(run,'06_critique_revision',
            'Review the previous mapping using held-out execution results and source semantics. '
            'Use exactly the allowed transform names: strip, upper, lower, digits, abn, acn, postcode; '
            'minimum_field_confidence is fixed at 0.8 by engine policy; never lower it. '
            'or objects with op date/enum/split/regex. Never use "string" or "string strip". '
            'Field confidence measures mapping/parse correctness, NOT coverage, entity linking, or publisher reliability. '
            'A sparse explicitly labelled ABN column can have high mapping confidence; absent values emit nothing. '
            'The abn transform already normalizes spaces and enforces checksum, rejecting compound identifiers. '
            'For labelled free text, a regex extracting exactly one labelled ABN is allowed; if multiple matches '
            'exist, the engine refuses the field. Do not reject a safe single-identifier extraction solely because '
            'other rows contain multiple suppliers. Preserve ambiguous names as unmapped. '
            'For an explicit list of supplier ABNs, split then validate each ABN, or regex with all:true then abn; '
            'set separate_entities:true on the identifier mapping to emit separately scoped identifier-only claims. '
            'No names/addresses are assigned to those parties. This safe capability resolves compound identifier lists. '
            'Correct invalid transforms, weak semantic mappings, and fields with poor coverage. '
            'Return {config:<complete revised config>, changes:[{field,before,after,reason}], '
            'semantic_risks:[...], requires_human_review:true}. Explicitly retain mappings only with defensible evidence. '
            'If no correction is needed, return config unchanged and explain that in changes.',
            dict(context, previous_proposal=config, holdout_validation=report,
                 heldout_field_profile=profile(holdout)),
            schema={'type':'object','properties':{'config':schema,'changes':{'type':'array','items':{'type':'object'}},
                   'semantic_risks':{'type':'array','items':{'type':'string'}},'requires_human_review':{'type':'boolean'}},
                   'required':['config','changes','semantic_risks','requires_human_review'],'additionalProperties':False})
        try:
            config = unwrap_config(revised['config'])
            validate_config(config,rows,ontology)
        except (ValueError, TypeError, KeyError) as exc:
            checkpoint('06b_invalid_revision',{'error':str(exc),'raw_revision':revised})
            repaired = model.call(run,'06c_repair_config',
                'Your revision is not executable. Return ONLY the mapping object itself, NOT a wrapper or JSON string. '
                'minimum_field_confidence must equal 0.8; the agent cannot lower this policy boundary. '
                'Fix the stated structural/transform errors. Use exact transform strings strip, upper, lower, digits, '
                'abn, acn, postcode or objects with op regex/date/enum/split. Keep conservative semantics. '
                'Confidence is mapping correctness, never coverage. Sparse labelled ABNs still map with high '
                'confidence if checksum validated. Unsupported or ambiguous fields remain unmapped.',
                {'ontology':ontology,'parser':parser,'error':str(exc),'previous':revised,'holdout_execution':report,
                 'source':source,'training':profile(train)},schema=schema)
            config = unwrap_config(repaired)
            validate_config(config,rows,ontology)
            checkpoint('06c_repair_config',config)
        config['parser'] = parser
        config['source_id'] = source['source_id']
        config['ontology_sha256'] = digest(ontology)
        config['generated_by'] = {'kind':'multi_step_runtime_agent','model':model.model,'run_identity':identity}
        config['review'] = {'status':'pending','reviewer':None,'approved_config_sha256':None}
        checkpoint('06_critique_revision',revised)
        final_report = validation(rows,list(range(1,len(rows)+1)),config,source,ontology)
        if final_report.get('fatal_error'):
            raise ValueError(final_report['fatal_error'])
        if final_report['observation_count'] == 0 and config.get('mappings'):
            checkpoint('07a_zero_yield',final_report)
            repaired = model.call(run,'07b_zero_yield_repair',
                'Execution produced ZERO observations. Inspect per-field validation and the raw samples. '
                'minimum_field_confidence must equal 0.8; this is a fixed policy boundary. '
                'Return only a complete executable mapping object. Correct actual parser/regex/transform issues '
                'and separate mapping confidence from coverage. Regex patterns sometimes contain double-escaped '
                'backslashes: prefer character classes like [0-9], [ ] and literal label text where possible. '
                'For explicit labelled supplier ABNs, regex all:true then abn with separate_entities:true is safe: '
                'each identifier gets an isolated claim and no names/addresses are paired. '
                'Use evidence to justify confidence; do not promote unsupported semantics merely to increase yield. '
                'If the source is genuinely unmappable, return mappings:[] and explain in unmapped_fields.',
                {'previous':config,'execution':final_report,'training':profile(train),
                 'ontology':ontology,'parser':parser,'source':source},schema=schema)
            config = unwrap_config(repaired)
            config.update(parser=parser,source_id=source['source_id'],ontology_sha256=digest(ontology),
                generated_by={'kind':'multi_step_runtime_agent','model':model.model,'run_identity':identity},
                review={'status':'pending','reviewer':None,'approved_config_sha256':None})
            validate_config(config,rows,ontology)
            final_report=validation(rows,list(range(1,len(rows)+1)),config,source,ontology)
            if final_report.get('fatal_error'):
                raise ValueError(final_report['fatal_error'])
            checkpoint('07b_zero_yield_repair',{'config':config,'validation':final_report})
        identifier_fields={'entity.abn','entity.acn','entity.nzbn'}
        if final_report['observation_count']==0 and any(
            f.get('canonical_field') in identifier_fields and f.get('valid_emissions',0)>0
            for f in final_report.get('per_field_validation',[])):
            verified=model.call(run,'07c_verified_mapping_review',
                'Review the corrected mapping AFTER it has actually executed. The earlier unverified parse '
                'is no longer the only evidence: use the supplied per-field diagnostic emission/checksum counts. '
                'Assess semantic mapping confidence separately from coverage and rejected malformed values. '
                'An explicit ACN/ABN label, a label-specific extraction, and passing checksums support mapping '
                'correctness; mixed labels in the same column should be filtered rather than conflated. '
                'Do not infer unlabelled identifiers or promote ambiguous legal names/addresses. '
                'Return only the complete config; minimum_field_confidence is fixed at 0.8. '
                'Confidence must reflect this new execution evidence, not the obsolete failed transform.',
                {'config':config,'corrected_execution':final_report,'ontology':ontology,
                 'source':source,'training':profile(train)},schema=schema)
            config=unwrap_config(verified)
            config.update(parser=parser,source_id=source['source_id'],ontology_sha256=digest(ontology),
                generated_by={'kind':'multi_step_runtime_agent','model':model.model,'run_identity':identity},
                review={'status':'pending','reviewer':None,'approved_config_sha256':None})
            validate_config(config,rows,ontology)
            final_report=validation(rows,list(range(1,len(rows)+1)),config,source,ontology)
            checkpoint('07c_verified_mapping_review',{'config':config,'validation':final_report})
        validate_config(config,rows,ontology)
        checkpoint('07_revalidation',final_report)
        save(root/'outputs/mappings'/f"{source['source_id']}.json",config)
        observations, errors = extract(rows,config,source,ontology)
        # Draft outputs are allowed for assessment; no pending config is silently production-approved.
        for obs in observations:
            obs['mapping_review_status'] = 'pending'
        jsonl(root/'outputs/observations'/f"{source['source_id']}.jsonl",observations)
        jsonl(root/'outputs/extraction_errors'/f"{source['source_id']}.jsonl",errors)
        jsonl(root/'outputs/record_inventory'/f"{source['source_id']}.jsonl",inventory(rows,source,config,observations))
        state.update({'status':'awaiting_human_approval','record_count':len(rows),'observation_count':len(observations),
                      'elapsed_seconds_this_invocation':time.monotonic()-started,
                      'config_sha256':digest(config),'step_count':len(set(state['steps'])|{'08_review_packet'}),
                      'weakest_step':'semantic inference; checksum/type validation cannot establish field meaning'})
        checkpoint('08_review_packet', {'source':source,'config':config,'validation':final_report,
            'warnings':revised.get('semantic_risks',[]), 'draft':True,
            'review_instructions':'Inspect roles, source evidence, temporal proxy, field coverage, unmapped fields and samples. Approve the exact config hash or reject with reason.'})
        state['status'] = 'awaiting_human_approval'
        state.pop('error',None)
        save(state_path,state)
        return state
    except Exception as exc:
        state.update(status='failed',error=str(exc),elapsed_seconds_this_invocation=time.monotonic()-started)
        save(state_path,state)
        raise
