#!/usr/bin/env python3
"""Pre-commit validation script for FUSED-CORE knowledge base.

Checks all machine-enforceable invariants and exits non-zero on any failure.
"""

import json
import hashlib
import sys
from pathlib import Path
from collections import defaultdict


def load_json(path):
    """Load JSON file, return None if not found."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def compute_hash(obj):
    """Compute sha256 of sorted JSON (excluding hash field)."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if k != 'hash'}
    content = json.dumps(obj, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(content.encode()).hexdigest()



def get_array_ids(module_data, arrays):
    """Get IDs from specified array fields."""
    ids = set()
    for arr_name in arrays:
        if arr_name in module_data:
            for item in module_data[arr_name]:
                if isinstance(item, dict) and 'id' in item:
                    ids.add(item['id'])
    return ids


def validate_all():
    """Run all validation checks."""
    # Script lives in scripts/migrations/; knowledge data in brendbot/knowledge/.
    base_dir = Path(__file__).resolve().parent.parent.parent / "brendbot" / "knowledge"
    manifest_path = base_dir / 'MANIFEST.json'
    manifest = load_json(manifest_path)

    if not manifest or 'modules' not in manifest:
        print('FAIL: Cannot load or parse MANIFEST.json')
        return False

    module_ids = {m['id'] for m in manifest['modules']}
    all_ids = defaultdict(set)  # module_id -> set of IDs
    errors = []
    warnings = []

    # Load all modules and collect IDs
    modules = {}
    for mod_info in manifest['modules']:
        mod_id = mod_info['id']
        mod_file = base_dir / mod_info['file']
        mod_data = load_json(mod_file)

        if mod_data is None:
            errors.append(f'Module {mod_id}: Cannot load {mod_file}')
            continue

        modules[mod_id] = (mod_data, mod_file)

        # Collect IDs based on module type
        if mod_id == 'IMAGEGEN':
            # IMAGEGEN has non-standard structure, skip ID collection
            pass
        else:
            # Collect IDs from BUILDSCI-style arrays. ops/gates/protos
            # were only used by the deleted PERSONALITY/GOVERNANCE modules
            # and are no longer expected anywhere in MANIFEST.
            all_ids[mod_id].update(get_array_ids(mod_data, ['defs', 'thms']))

    # Flatten all defined IDs across modules
    all_defined_ids = set()
    for ids in all_ids.values():
        all_defined_ids.update(ids)

    # Validation checks
    print('Validating FUSED-CORE knowledge base...\n')

    # 1. Schema Conformance
    print('1. Schema Conformance')
    for mod_id, (mod_data, mod_file) in modules.items():
        if mod_id == 'IMAGEGEN':
            continue

        for item_type in ['defs', 'thms']:
            if item_type not in mod_data:
                continue
            for item in mod_data[item_type]:
                if 'id' not in item:
                    errors.append(f'{mod_id}: {item_type} missing id field')

                if item_type == 'defs' and 't' not in item and 'term' not in item:
                    errors.append(f'{mod_id}: def {item.get("id")} missing t/term')
                elif item_type == 'thms' and 'stmt' not in item and 'd' not in item and 'f' not in item and 'formal' not in item:
                    errors.append(f'{mod_id}: thm {item.get("id")} missing stmt/d/f/formal')

        if 'xlinks' in mod_data:
            for xlink in mod_data['xlinks']:
                has_from = 'from' in xlink or 'fr' in xlink
                has_to = 'to' in xlink
                if not (has_from and has_to):
                    errors.append(f'{mod_id}: xlink missing from/fr or to')

    if not errors:
        print('  PASS: All definitions have required fields')

    # 2. Reference Resolution
    print('2. Reference Resolution')
    ref_errors = []
    for mod_id, (mod_data, _) in modules.items():
        if mod_id == 'IMAGEGEN':
            continue

        # Check uses, xlinks, triggers
        for item_type in ['defs', 'thms']:
            if item_type not in mod_data:
                continue
            for item in mod_data[item_type]:
                for ref_field in ['uses', 'triggers']:
                    if ref_field in item:
                        for ref_id in item[ref_field]:
                            if ref_id not in all_defined_ids:
                                ref_errors.append(
                                    f'{mod_id}: {item.get("id")}.{ref_field} -> {ref_id} (undefined)'
                                )

        # Check xlinks (from/fr and to — to is a list)
        if 'xlinks' in mod_data:
            for xlink in mod_data['xlinks']:
                src = xlink.get('from') or xlink.get('fr')
                dst_list = xlink.get('to', [])
                if isinstance(dst_list, str):
                    dst_list = [dst_list]
                if src and src not in all_defined_ids:
                    ref_errors.append(f'{mod_id}: xlink from {src} (undefined)')
                for dst in dst_list:
                    if dst not in all_defined_ids:
                        ref_errors.append(f'{mod_id}: xlink to {dst} (undefined)')

    if ref_errors:
        errors.extend(ref_errors)
        print(f'  FAIL: {len(ref_errors)} reference(s) unresolved')
    else:
        print('  PASS: All references resolve to defined IDs')

    # 3. Hash Chain Verification
    print('3. Hash Chain Verification')
    hash_errors = []
    for mod_id, (mod_data, mod_file) in modules.items():
        if 'hash' not in mod_data:
            continue

        hash_val = mod_data['hash']
        if hash_val in ('recompute-required', 'sha256:TO_SET'):
            continue

        computed = compute_hash(mod_data)
        if computed != hash_val:
            hash_errors.append(f'{mod_id}: hash mismatch (expected {hash_val}, got {computed})')

    if hash_errors:
        errors.extend(hash_errors)
        print(f'  FAIL: {len(hash_errors)} hash mismatch(es)')
    else:
        print('  PASS: All hash chains verified')

    # 4. Crosslink Bidirectionality
    print('4. Crosslink Bidirectionality')
    xlinks_by_pair = defaultdict(list)
    for mod_id, (mod_data, _) in modules.items():
        if 'xlinks' not in mod_data:
            continue
        for xlink in mod_data['xlinks']:
            src = xlink.get('from') or xlink.get('fr')
            dst_list = xlink.get('to', [])
            if isinstance(dst_list, str):
                dst_list = [dst_list]
            for dst in dst_list:
                xlinks_by_pair[(src, dst)].append(mod_id)

    unidirectional = []
    for (src, dst), mods in xlinks_by_pair.items():
        if (dst, src) not in xlinks_by_pair:
            unidirectional.append(f'{src} -> {dst} (in {mods[0]})')

    if unidirectional:
        warnings.extend([f'Unidirectional: {ul}' for ul in unidirectional])
        print(f'  WARN: {len(unidirectional)} unidirectional link(s)')
    else:
        print('  PASS: All crosslinks are bidirectional')

    # 5. Source Map Coverage
    print('5. Source Map Coverage')
    source_warnings = []
    for mod_id, (mod_data, _) in modules.items():
        if mod_id == 'IMAGEGEN':
            continue

        src_map = mod_data.get('prov', {}).get('src_map') or mod_data.get('src_map', {})

        for item_type in ['defs', 'thms']:
            if item_type not in mod_data:
                continue
            for item in mod_data[item_type]:
                source = item.get('s') or item.get('source') or item.get('src')
                if source:
                    sources = source if isinstance(source, list) else [source]
                    for src_ref in sources:
                        if src_ref not in src_map:
                            source_warnings.append(f'{mod_id}: {item.get("id")} -> source {src_ref} (undefined)')

    if source_warnings:
        warnings.extend(source_warnings)
        print(f'  WARN: {len(source_warnings)} undefined source(s)')
    else:
        print('  PASS: All sources defined in src_map')

    # 6. Dangling Dependencies
    print('6. Dangling Dependencies')
    dep_errors = []
    for mod_id, (mod_data, _) in modules.items():
        if 'deps' not in mod_data:
            continue
        for dep in mod_data['deps']:
            if dep not in module_ids:
                dep_errors.append(f'{mod_id}: depends on {dep} (module not in MANIFEST)')

    if dep_errors:
        errors.extend(dep_errors)
        print(f'  FAIL: {len(dep_errors)} dangling dependenc(ies)')
    else:
        print('  PASS: All dependencies resolvable')

    # Summary
    print(f'\n{"="*60}')
    print(f'Errors: {len(errors)}')
    print(f'Warnings: {len(warnings)}')

    if errors:
        print(f'\nFailed checks:')
        for err in errors[:10]:
            print(f'  - {err}')
        if len(errors) > 10:
            print(f'  ... and {len(errors) - 10} more')
        return False

    if warnings:
        print(f'\nWarnings:')
        for warn in warnings[:5]:
            print(f'  - {warn}')
        if len(warnings) > 5:
            print(f'  ... and {len(warnings) - 5} more')

    print('\nValidation PASSED')
    return True


if __name__ == '__main__':
    success = validate_all()
    sys.exit(0 if success else 1)
