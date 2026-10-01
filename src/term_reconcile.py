"""Compare AI term candidates with project references without choosing translations."""

from collections import Counter, defaultdict
import json
import re


NAME_FIELD = re.compile(r'(?:^|\.)(?:name|title|fullName)$', re.I)


def reconcile(reports, entries, glossary):
    grouped = {}
    for report in reports:
        if (report.get('schema_version') != 1 or
                report.get('scope') != 'semantic_review_only_no_dictionary_changes' or
                not isinstance(report.get('candidates'), list)):
            raise ValueError('Expected a semantic review report, not raw model output')
        for candidate in report['candidates']:
            source = candidate.get('source')
            if (not isinstance(source, str) or not source or
                    not isinstance(candidate.get('category'), str) or
                    not isinstance(candidate.get('evidence'), list) or
                    not isinstance(candidate.get('suggested_translations'), dict)):
                raise ValueError('Invalid semantic candidate')
            row = grouped.setdefault(source, {'categories': set(), 'evidence': {},
                                              'model_suggestions': set(), 'model_abstained': False})
            row['categories'].add(candidate['category'])
            for target, count in candidate['suggested_translations'].items():
                if not isinstance(target, str) or type(count) is not int or count < 1:
                    raise ValueError('Invalid model suggestion')
                # Repairs overlap the main pass; repeat counts are not votes.
                if target:
                    row['model_suggestions'].add(target)
                else:
                    row['model_abstained'] = True
            for evidence in candidate['evidence']:
                if (not isinstance(evidence, dict) or
                        not isinstance(evidence.get('row_id'), str) or
                        not isinstance(evidence.get('location'), dict)):
                    raise ValueError('Invalid evidence identity')
                # Different snapshots may reuse a row ID with revised context.
                # Only identical evidence is redundant; preserve both versions.
                identity = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                row['evidence'][identity] = evidence

    references = defaultdict(list)
    for entry in entries:
        # A fragment in dialogue/description is not the translation of that
        # whole field. Only exact named fields are reference translations.
        if entry.source in grouped and NAME_FIELD.search(entry.location['field']):
            references[entry.source].append({'location': entry.location,
                                             'translation': entry.translation})
    known = defaultdict(set)
    for term in glossary:
        if term.get('scope'):
            raise ValueError('Scoped policies require contextual review; use unscoped glossary inputs')
        known[term['source']].add(term['target'])

    output = []
    for source, row in sorted(grouped.items()):
        refs = sorted(references[source], key=lambda x: json.dumps(x, sort_keys=True))
        master_targets = sorted({ref['translation'] for ref in refs if ref['translation']})
        glossary_targets = sorted(known[source])
        reference_targets = set(master_targets) | set(glossary_targets)
        if len(reference_targets) > 1:
            status = 'reference_conflict'
        elif glossary_targets:
            status = 'glossary_reference'
        elif master_targets:
            status = 'master_reference'
        else:
            status = 'needs_review'
        suggestions = sorted(row['model_suggestions'])
        output.append({'source': source, 'categories': sorted(row['categories']),
                       'status': status, 'glossary_targets': glossary_targets,
                       'master_targets': master_targets, 'master_references': refs,
                       'untranslated_master_references': sum(not ref['translation'] for ref in refs),
                       'model_suggestions': suggestions,
                       'model_abstained': row['model_abstained'],
                       'model_reference_disagreement': bool(reference_targets and
                           any(target not in reference_targets for target in suggestions)),
                       'evidence': [row['evidence'][key] for key in sorted(row['evidence'])]})
    return {'schema_version': 1, 'scope': 'term_reference_review_only_no_dictionary_changes',
            'notes': ['Exact Master name/title/fullName fields are references, not automatic approvals.',
                      'Model category and suggestions remain unreviewed; no majority vote or first-wins.',
                      'Historical extraction evidence is retained; current Master references are looked up anew.'],
            'summary': {'candidates': len(output),
                        'by_status': dict(sorted(Counter(r['status'] for r in output).items())),
                        'model_reference_disagreement': sum(r['model_reference_disagreement'] for r in output)},
            'candidates': output}
