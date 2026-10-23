#!/usr/bin/env python3
"""Scrub fixed owner binding from an already copied, disposable public snapshot.

Never reads private runtime state. A remaining activation payload fails the leak
scan; it is not silently accepted. No git/network operation or publication here.
"""
import argparse
import ast
from pathlib import Path
import shutil
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

PUBLIC_SCHEDULE='public-owner-fitness-schedule'
PRIVATE_NAMES={'schedule_occurrence_activation.json','schedule_occurrences.sqlite3',
               'native_fitness_activation.json','native_fitness_handoff.json','native_fitness.lock'}
PRIVATE_FEATURE_PATHS={'davosbot/budget_recurring_service.py','tests/test_budget_recurring_service.py','scripts/budget_live_workflow_validate.py','.github/workflows/budget-data-dispatch.yml','.github/workflows/budget-validator.yml','scripts/budget_workflow_validate.py','tests/test_budget_workflow_validate.py','davosbot/recurring_budget.py','davosbot/budget_validator_core.py','tests/test_recurring_budget.py','davosbot/budget_once.py','davosbot/budget_service.py','tests/test_budget_once.py','tests/test_budget_service.py','davosbot/native_fitness.py','davosbot/native_fitness_worker.py','davosbot/native_workout_review.py','tests/test_native_fitness.py','tests/test_native_fitness_worker.py','tests/test_native_workout_review.py'}
TEXT_SUFFIXES={'.md','.py','.txt','.json','.yml','.yaml','.js','.ps1','.sh','.example','.gitignore','.gitattributes'}


def binding(root):
    module=root/'davosbot/native_fitness.py'
    if not module.exists():return None
    values=[]
    for statement in ast.parse(module.read_text(encoding='utf-8')).body:
        if isinstance(statement,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='SCHEDULE' for t in statement.targets):
            if not isinstance(statement.value,ast.Constant) or not isinstance(statement.value.value,str):raise ValueError('unsupported_schedule_binding')
            values.append(statement.value.value)
    if len(values)!=1 or not values[0]:raise ValueError('missing_unique_schedule_binding')
    return values[0]


def scrub(root):
    root=Path(root).resolve()
    if root==Path(__file__).resolve().parents[1] or (root/'.git').exists():raise ValueError('disposable_snapshot_required')
    # Reject all links before traversal or writes, including files pointing outside.
    entries=list(root.rglob('*'))
    if any(p.is_symlink() for p in entries):raise ValueError('snapshot_symlink_rejected')
    private=binding(root);removed=[];changed=0
    for p in sorted(entries,key=lambda x:len(x.parts)):
        if not p.exists():continue
        if p.relative_to(root).as_posix() in PRIVATE_FEATURE_PATHS or p.name in {'.work_bridge','.budget_once','.budget_recurring'} or p.name in PRIVATE_NAMES or re.search(r'\.(?:db|sqlite|sqlite3)(?:-(?:wal|shm))?$',p.name):
            removed.append(p.relative_to(root).as_posix())
            if p.is_dir():shutil.rmtree(p)
            else:p.unlink()
    main=root/'davosbot/main.py'
    if main.exists():
        text=main.read_text(encoding='utf-8')
        lines=["from .native_fitness_worker import start_native_fitness, stop_native_fitness\n",
               "    start_native_fitness()  # No worker without separately reviewed native activation.\n",
               "        stop_native_fitness(timeout=3.0)\n"]
        if 'native_fitness_worker' in text or 'start_native_fitness(' in text or 'stop_native_fitness(' in text:
            if any(text.count(line)!=1 for line in lines):raise ValueError('unrecognized_private_startup_hook')
            for line in lines:text=text.replace(line,'')
            main.write_text(text,encoding='utf-8')
    if main.exists():
        text=main.read_text(encoding='utf-8')
        lines=["from .budget_service import start_budget_once, stop_budget_once\n",
               "    start_budget_once()  # One attempt only with separately approved budget activation.\n",
               "        stop_budget_once(timeout=3.0)\n"]
        if 'budget_service' in text or 'start_budget_once(' in text or 'stop_budget_once(' in text:
            if any(text.count(line)!=1 for line in lines):raise ValueError('unrecognized_private_budget_hook')
            for line in lines:text=text.replace(line,'')
            main.write_text(text,encoding='utf-8')
    if main.exists():
        text=main.read_text(encoding='utf-8')
        lines=["from .budget_recurring_service import start_budget_recurring, stop_budget_recurring\n",
               "    start_budget_recurring()  # No receiver without distinct reviewed pilot authority.\n",
               "        stop_budget_recurring(timeout=3.0)\n"]
        if 'budget_recurring_service' in text or 'start_budget_recurring(' in text or 'stop_budget_recurring(' in text:
            if any(text.count(line)!=1 for line in lines):raise ValueError('unrecognized_recurring_budget_hook')
            for line in lines:text=text.replace(line,'')
            main.write_text(text,encoding='utf-8')
    core=root/'davosbot/work_occurrences.py'
    if core.exists():
        text=core.read_text(encoding='utf-8');lines=text.splitlines(keepends=True)
        for node in ast.parse(text).body:
            if isinstance(node,ast.FunctionDef) and node.name=='execute_native':
                lines[node.lineno-1:node.end_lineno]=["def execute_native(*args, **kwargs):\n    raise ValueError('private_native_scheduler_unavailable')\n"]
                break
        core.write_text(''.join(lines),encoding='utf-8')
    if private and private!=PUBLIC_SCHEDULE:
        for p in root.rglob('*'):
            if not p.is_file() or (p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {'.gitignore','.gitattributes'}):continue
            text=p.read_text(encoding='utf-8')
            if private in text:
                p.write_text(text.replace(private,PUBLIC_SCHEDULE),encoding='utf-8');changed+=1
    # The exact private ID must never survive in an unrecognized or binary file.
    if private and private != PUBLIC_SCHEDULE:
        for p in root.rglob('*'):
            if p.is_file() and private.encode('utf-8') in p.read_bytes():
                raise ValueError('private_binding_bytes_remaining')
    # Runtime-binding markers in every file, including tests, fail closed.
    from scripts.scan_public_snapshot import scan_snapshot
    issues=scan_snapshot(root)
    binding_issues=[i for i in issues if 'binding' in i.label or 'activation' in i.label]
    if binding_issues:raise ValueError('private_binding_payload_remaining')
    return {'changed_files':changed,'removed_paths':removed,'private_values_printed':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('snapshot',type=Path);args=parser.parse_args()
    try:
        result=scrub(args.snapshot)
        print('Runtime binding scrub passed; changed files:',result['changed_files'],'removed artifacts:',len(result['removed_paths']))
    except (ValueError,OSError,SyntaxError) as error:
        parser.exit(1,'Runtime binding scrub failed: '+type(error).__name__+'\n')
