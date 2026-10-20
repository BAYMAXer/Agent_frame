"""Human browser demonstrations with frozen observations and explicit review.

This module performs no browser or model I/O. The controller must prepare each
observation before dispatching the human action, then provide its real result.
Human review supplies SFT supervision; it never supplies an independent reward.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

from .actions import Action, action_from_decision, get_action_spec, parse_decision, validate_action
from .context import PromptCompiler
from .env import Observation
from .model.decision_model import encode_image
from .prompts import build_system_prompt
from .scene import scene_hint
from .training_events import TrainingEventWriter
from .trajectory import TRAJECTORY_VERSION, dump_trajectory
from .verifiers import CompletionVerifier

_COORDINATES = {'x', 'y', 'x1', 'y1', 'x2', 'y2', 'coordinate_space', 'observation_metadata',
                'grounding_failed', 'button', 'click_count'}
_BAD_STATUS = {'rejected', 'failed', 'failure', 'uncertain', 'error', 'cancelled'}
_GOOD_STATUS = {'executed', 'completed', 'already_downloaded', 'observed', 'ok'}
_OPTIONAL_ARGS = {
    'click': {'target_ref', 'element_id'}, 'double_click': {'target_ref', 'element_id'},
    'right_click': {'target_ref', 'element_id'},
    'type': {'target_ref', 'element_id', 'overwrite'},
    'select': {'target_ref', 'element_id'}, 'scroll': {'target', 'target_ref', 'element_id'},
    'get_page_source': {'target_ref'}, 'handle_dialog': {'prompt_text', 'dialog_id'},
    'close_tab': {'tab_id'},
}


def _record_observation(observation, identifier):
    if not isinstance(observation, Observation):
        raise TypeError('Observations must use the framework Observation type')
    return {'id': identifier, 'context': copy.deepcopy(observation.context),
            'text': observation.text, 'info': copy.deepcopy(observation.info),
            'screenshot': encode_image(observation.screenshot)}


def _fingerprint(observation):
    # The ID is recorder-owned; browser metadata and image bytes are immutable evidence.
    value = copy.deepcopy({key: val for key, val in observation.items() if key != 'id'})
    value.get('info', {}).pop('exposed_refs', None)  # Compiler output, not browser state.
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _action(value):
    if isinstance(value, Action):
        return Action(value.action, copy.deepcopy(value.args))
    if isinstance(value, dict):
        if isinstance(value.get('args'), dict) and value.get('action'):
            return Action(str(value['action']), copy.deepcopy(value['args']))
        parsed = action_from_decision(value)
        if parsed is not None:
            return parsed
    raise ValueError('Expected Action or a canonical action/next_action object')


def _nodes(before):
    return [node for block in before.get('context', []) if block.get('kind') == 'browser_ax'
            for node in block.get('nodes', [])]


def _target_node(action, before, metadata):
    ref = action.args.get('target_ref') or metadata.get('target_ref')
    element_id = action.args.get('element_id') or metadata.get('element_id')
    matched = [node for node in _nodes(before)
               if (ref and node.get('ref') == ref) or (element_id and node.get('id') == element_id)]
    return matched[0] if len(matched) == 1 else None


def _sensitive(action, before, metadata):
    if any(metadata.get(key) is True for key in ('sensitive', 'contains_secrets', 'is_password')):
        return True
    node = _target_node(action, before, metadata) or {}
    attrs = node.get('attributes') or {}
    if not isinstance(attrs, dict):
        attrs = {}
    states = node.get('states') or {}
    types = [metadata.get('input_type'), metadata.get('type'), node.get('input_type'),
             node.get('type'), attrs.get('type')]
    return any(str(value).lower() == 'password' for value in types) or bool(states.get('protected'))


def _semantic_action(action, before, metadata, exposed_refs, *, target=None, intent=None):
    spec = get_action_spec(action.action)
    if spec is None:
        return None, 'unknown_action'
    allowed = set(spec.params) | _OPTIONAL_ARGS.get(action.action, set())
    args = {key: copy.deepcopy(value) for key, value in action.args.items()
            if key in allowed and key not in _COORDINATES}
    node = _target_node(action, before, metadata)
    label = (target or args.get('target') or metadata.get('target_label')
             or metadata.get('semantic_target') or (node or {}).get('name'))
    if label and 'target' in allowed:
        if not isinstance(label, str) or not label.strip():
            return None, 'target_must_be_text'
        args['target'] = label.strip()
    if node:
        ref = node.get('ref')
        if ref in exposed_refs and 'target_ref' in allowed:
            args['target_ref'] = ref
    # Hidden or stale refs are executor evidence, never policy targets.
    if args.get('target_ref') not in exposed_refs:
        args.pop('target_ref', None)
    if args.get('element_id'):
        visible_ids = {n.get('id') for n in _nodes(before) if n.get('ref') in exposed_refs}
        if args['element_id'] not in visible_ids:
            args.pop('element_id', None)
    candidate = Action(action.action, args)
    error = validate_action(candidate)
    if error:
        return None, 'missing_semantic_target' if any(
            key in spec.params and not args.get(key) for key in ('target', 'from_target', 'to_target')) else error
    return {'action': candidate.action, 'args': candidate.args}, None


class HumanDemonstrationSession:
    """Filesystem recorder used by a controller; no model calls or event execution."""

    def __init__(self, task, directory, completion_spec=None, private_completion_spec=None, *,
                 session_id=None, context_config=None, os_name='windows', system_prompt=None):
        if not isinstance(task, str) or not task.strip():
            raise ValueError('A non-empty task is required')
        self.task = task
        self.directory = Path(directory).resolve()
        self.session_id = session_id or uuid.uuid4().hex
        self.verifier = CompletionVerifier(task, completion_spec, private_completion_spec)
        self.compiler = PromptCompiler(context_config)
        self.system_prompt = system_prompt or build_system_prompt(os_name=os_name, browser_only=True)
        self.steps = []
        self._prepared = {}
        self._stopped = False
        self._closed = False
        self._claimed_complete = False
        self._outcome = None
        self.writer = TrainingEventWriter(self.directory / 'events.jsonl', run_id=self.session_id,
            manifest={'actor': 'human', 'policy': 'external', 'task': task,
                      'grounder_trainable': False, 'compiler_version': self.compiler.version,
                      'public_completion': self.verifier.public_spec})

    def _assert_open(self):
        if self._closed:
            raise ValueError('Demonstration session is closed')

    def prepare_observation(self, before):
        """Freeze the policy input before the controller executes any action.

        Preparations remain in memory until recording, so an explicitly sensitive
        action can quarantine the entire frame instead of writing credentials.
        Returned fields may be passed as frozen_policy_input/frozen_prompt_metadata.
        """
        self._assert_open()
        if self._stopped:
            raise ValueError('Recording has stopped')
        preparation_id = uuid.uuid4().hex
        observation_id = before.info.get('observation_id') or f'{self.session_id}:o{len(self._prepared) + 1}'
        observation = _record_observation(before, observation_id)
        blocks = copy.deepcopy(before.context)
        # Match the inference pipeline's public browser observation block.
        # This is frozen before dispatch and contains no evaluator-only data.
        browser_events = {key: copy.deepcopy(before.info[key]) for key in (
            'dialog', 'tabs', 'active_modals', 'browser_events', 'artifacts', 'screenshot_stale')
            if key in before.info}
        browser_events_text = json.dumps(browser_events, ensure_ascii=False) if browser_events else ''
        if browser_events_text:
            blocks.insert(0, {'kind': 'browser_events', 'text': browser_events_text})
        blocks.insert(0, {'kind': 'scene', 'text': '当前场景: ' + json.dumps(scene_hint(before), ensure_ascii=False)})
        if before.text:
            blocks.append({'kind': 'desktop_a11y', 'text': before.text})
        earlier_actions = [copy.deepcopy(step['history_entry']) for step in self.steps[-8:]]
        history = '\n'.join(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) for entry in earlier_actions)
        user_text, images, prompt_metadata = self.compiler.compile(
            system=self.system_prompt, task=self.task,
            memory=history, memory_title='近期人工操作（实际记录，非完成证据）', recent_memory=True,
            blocks=blocks, auxiliary='',
            required='按总任务继续操作；人工示范未提供子任务计划。', required_title='当前子任务',
            images=[('【当前截图 S(t)，原图】', observation['screenshot'])] if observation['screenshot'] else [],
            require_image=bool(observation['screenshot']))
        # Non-AX blocks also have a budget. Conservatively require the complete
        # browser state to survive before exposing its opaque IDs as labels.
        browser_state_visible = bool(browser_events_text and browser_events_text in user_text)
        prompt_metadata['exposed_tab_ids'] = [tab['tab_id'] for tab in browser_events.get('tabs', [])
            if isinstance(tab, dict) and isinstance(tab.get('tab_id'), str)] if browser_state_visible else []
        dialog = browser_events.get('dialog') or {}
        dialog_id = (dialog.get('id') or dialog.get('dialog_id')) if isinstance(dialog, dict) else None
        prompt_metadata['exposed_dialog_ids'] = [dialog_id] if browser_state_visible and dialog_id else []
        content = [{'type': 'text', 'text': user_text}]
        for label, screenshot in images:
            content.extend([{'type': 'text', 'text': label},
                            {'type': 'image_url', 'image_url': {'url': screenshot}}])
        policy_input = [{'role': 'system', 'content': self.system_prompt}, {'role': 'user', 'content': content}]
        if not observation['screenshot']:
            # A blocking native dialog can prevent capture. Keep its real action
            # for recovery/audit, but do not invent a current image for training.
            policy_input = None
            prompt_metadata.update(exposed_refs=[], usable_for_training=False,
                                   exclusion_reason='missing_pre_action_image')
        prepared = {'preparation_id': preparation_id, 'observation_id': observation_id,
                    'policy_input': policy_input, 'prompt_metadata': prompt_metadata,
                    'observation': observation, 'fingerprint': _fingerprint(observation),
                    'prepared_at': time.time(), 'used': False}
        self._prepared[preparation_id] = prepared
        return copy.deepcopy({key: prepared[key] for key in
                              ('preparation_id', 'observation_id', 'policy_input', 'prompt_metadata')})

    def _get_preparation(self, before, metadata):
        supplied = metadata.get('preparation_id')
        candidates = ([self._prepared[supplied]] if supplied in self._prepared else
                      list(self._prepared.values()) if supplied is None else [])
        record = _record_observation(before, '')
        fingerprint = _fingerprint(record)
        candidates = [item for item in candidates if not item['used'] and item['fingerprint'] == fingerprint]
        frozen = metadata.get('frozen_policy_input')
        frozen_metadata = metadata.get('frozen_prompt_metadata')
        if frozen is not None:
            candidates = [item for item in candidates if item['policy_input'] == frozen]
        if frozen_metadata is not None:
            candidates = [item for item in candidates if item['prompt_metadata'] == frozen_metadata]
        if len(candidates) != 1:
            raise ValueError('Missing, ambiguous, reused or altered pre-action preparation')
        return candidates[0]

    def record_transition(self, action, before, after, execution_info=None, metadata=None):
        self._assert_open()
        if self._stopped:
            raise ValueError('Recording has stopped')
        action = _action(action)
        if metadata is not None and not isinstance(metadata, dict):
            raise ValueError('metadata must be an object')
        if execution_info is not None and not isinstance(execution_info, dict):
            raise ValueError('execution_info must be an object')
        metadata = copy.deepcopy(metadata or {})
        execution_info = copy.deepcopy(execution_info or {})
        prepared = self._get_preparation(before, metadata)
        step_id = f'h_{len(self.steps) + 1}'
        secret = _sensitive(action, prepared['observation'], metadata)
        observation_id = prepared['observation_id']
        next_observation = _record_observation(
            after, after.info.get('observation_id') or f'{self.session_id}:after{len(self.steps) + 1}')
        # Verification consumes actual observations; human labels cannot alter it.
        # Sensitive frames cannot be a source of archived URL/path evidence.
        # A later non-sensitive transition can supply fresh verifiable state.
        self.verifier.observe(Observation() if secret else after, {} if secret else execution_info)
        report = self.verifier.evaluate()
        clean_metadata = {key: value for key, value in metadata.items()
                          if key not in ('frozen_policy_input', 'frozen_prompt_metadata')}
        semantic, invalid = _semantic_action(action, prepared['observation'], clean_metadata,
                                             prepared['prompt_metadata']['exposed_refs'])
        raw_action = {'action': action.action, 'args': action.args}
        if secret:
            # Never persist typed passwords, screenshots, AX values, or raw notes for this transition.
            observation_id = f'{self.session_id}:{step_id}:redacted-before'
            raw_action = {'action': action.action, 'args': {'redacted': True}}
            observation = {'id': observation_id, 'screenshot': None, 'context': [], 'redacted': True}
            next_observation = {'id': f'{self.session_id}:{step_id}:redacted-after',
                                'screenshot': None, 'context': [], 'redacted': True}
            policy_input, prompt_metadata = None, {}
            semantic, invalid = None, 'sensitive_content'
            clean_metadata = {'sensitive': True}
            stored_execution = {'status': execution_info.get('status', 'unknown'), 'redacted': True}
            stored_report = {'status': report.get('status'), 'checks': [], 'reason': 'sensitive evidence omitted'}
        else:
            observation = prepared['observation']
            policy_input, prompt_metadata = prepared['policy_input'], prepared['prompt_metadata']
            stored_execution = execution_info
            stored_report = report
        history_entry = {'step_id': step_id, 'action': copy.deepcopy(semantic or raw_action),
                         'status': execution_info.get('status', 'unknown')}
        if semantic is None and not secret:
            history_entry['action'] = {'action': action.action, 'args': {}, 'semantic_target_unavailable': True}
        step = {'step_id': step_id, 'step': len(self.steps) + 1, 'actor': 'human',
                'observation': observation, 'next_observation': next_observation,
                'policy_input': policy_input, 'prompt_metadata': prompt_metadata,
                'raw_action': raw_action, 'action': semantic, 'semantic_error': invalid,
                'metadata': clean_metadata, 'execution_info': stored_execution,
                'prepared_at': prepared['prepared_at'], 'preparation_id': prepared['preparation_id'],
                'review': {'status': 'pending', 'keep': False, 'safe_for_training': False},
                'verification': stored_report, 'sensitive': secret, 'history_entry': history_entry}
        event = self.writer.emit('human_transition', actor_role='human', source='human',
            parent_decision_id=step_id, observation_id=observation_id, transition=step)
        stored = event['transition']
        stored['event_id'] = event['event_id']
        self.steps.append(stored)
        prepared['used'] = True
        # Drop original image/text buffers once frozen evidence has been persisted or redacted.
        prepared['policy_input'] = None
        prepared['observation'] = None
        return self._summary(stored)

    def review_step(self, step_id, *, keep, intent=None, expected_effect=None,
                    safe_for_training=True, target=None):
        self._assert_open()
        if type(keep) is not bool or type(safe_for_training) is not bool:
            raise ValueError('keep and safe_for_training must be booleans')
        step = next((item for item in self.steps if item['step_id'] == step_id), None)
        if step is None:
            raise KeyError(step_id)
        for value in (intent, expected_effect, target):
            if value is not None and (not isinstance(value, str) or len(value) > 4000):
                raise ValueError('Human annotations must be strings of at most 4000 characters')
        if keep and step['sensitive']:
            raise ValueError('Sensitive transitions cannot be approved for training')
        review = {'status': 'reviewed', 'keep': keep, 'safe_for_training': bool(keep and safe_for_training),
                  'intent': intent, 'expected_effect': expected_effect, 'target': target,
                  'annotation_source': 'human', 'annotation_timing': 'posthoc_review'}
        self.writer.emit('human_review', actor_role='human', source='human', parent_decision_id=step_id,
                         review=review)
        step['review'] = review
        if not step['sensitive']:
            semantic, error = _semantic_action(_action(step['raw_action']), step['observation'],
                step['metadata'], step['prompt_metadata'].get('exposed_refs', []), target=target, intent=intent)
            step['action'], step['semantic_error'] = semantic, error
        if self._stopped:
            self._save_archive()
        return self._summary(step)

    def _outcome_record(self):
        report = self.verifier.evaluate()
        status = report.get('status', 'unknown')
        known = bool(self.verifier.spec.get('checks')) and status in ('passed', 'failed')
        return {'success': status == 'passed', 'independent_verified': status == 'passed',
                'verification': report, 'evaluation_available': known,
                'reward_valid': known, 'reward': (1.0 if status == 'passed' else 0.0) if known else None,
                'score': 1.0 if status == 'passed' else 0.0,
                'reward_source': 'completion_contract' if known else 'unavailable',
                'human_claimed_complete': self._claimed_complete,
                'termination_reason': 'human_stopped' if status != 'passed' else 'verified'}

    def stop(self, claimed_complete=False, final_observation=None):
        self._assert_open()
        if type(claimed_complete) is not bool:
            raise ValueError('claimed_complete must be a boolean')
        if final_observation is not None and not (self.steps and self.steps[-1]['sensitive']):
            self.verifier.observe(final_observation)
        self._claimed_complete = self._claimed_complete or claimed_complete
        self._stopped = True
        self._outcome = self._outcome_record()
        self.writer.emit('human_stop', actor_role='human', source='human', claimed_complete=claimed_complete,
                         outcome=self._outcome)
        self._save_archive()
        return self.public_outcome()

    def public_outcome(self):
        outcome = self._outcome_record()
        outcome['verification'] = self.verifier.public_report(outcome['verification'])
        return outcome

    def _summary(self, step, *, include_observations=True):
        item = {'step_id': step['step_id'], 'step': step['step'],
                'action': copy.deepcopy(step['action'] or step['raw_action']),
                'semantic_error': step['semantic_error'], 'review': copy.deepcopy(step['review']),
                'sft_exclude_reason': step.get('metadata', {}).get('sft_exclude_reason'),
                'sensitive': step['sensitive'], 'execution_status': step['execution_info'].get('status', 'unknown')}
        if include_observations:
            item.update(observation=copy.deepcopy(step['observation']),
                        next_observation=copy.deepcopy(step['next_observation']))
        return item

    def list_steps(self):
        return [self._summary(step) for step in self.steps]

    def summary(self):
        """UI-facing review state; private evaluator targets are never exposed."""
        steps = []
        for step in self.steps:
            item = self._summary(step, include_observations=False)
            # The UI only needs asset references; sending every AX snapshot on
            # each update grows quadratically with the number of steps.
            before, after = step['observation'], step['next_observation']
            item.update(observation_id=before.get('id'), next_observation_id=after.get('id'),
                        screenshot=before.get('screenshot'), next_screenshot=after.get('screenshot'))
            review = step['review']
            reviewed = review.get('status') == 'reviewed'
            item.update(step=step['step_id'], step_index=step['step'],
                        keep=review.get('keep') if reviewed else None, reviewed=reviewed,
                        target=review.get('target') or (step.get('action') or {}).get('args', {}).get('target'),
                        intent=review.get('intent'), expected_effect=review.get('expected_effect'))
            steps.append(item)
        outcome = self.public_outcome()
        return {'session_id': self.session_id, 'task': self.task,
                'state': 'closed' if self._closed else 'review' if self._stopped else 'recording',
                'steps': steps, 'verification': outcome['verification'], 'outcome': outcome}

    def _save_archive(self):
        archived_steps = []
        for step in self.steps:
            item = copy.deepcopy(step)
            item.update(trainable=False, valid_action=bool(step['action']), decision_calls=[],
                        policy_output=None, action_id=step['step_id'])
            archived_steps.append(item)
        archive = {'version': TRAJECTORY_VERSION, 'task': {'id': self.session_id,
            'domain': 'human_browser', 'instruction': self.task}, 'outcome': self._outcome_record(),
            'steps': archived_steps, 'events': self.writer.events,
            'provenance': {'actor': 'human', 'policy': 'external', 'use_for_model_training': True,
                           'review_required': True, 'on_policy': False},
            'evaluation_context': {'completion_spec': self.verifier.spec},
            'run_manifest': {'run_id': self.session_id, 'actor': 'human', 'grounder_trainable': False,
                             'public_completion': self.verifier.public_spec}}
        dump_trajectory(self.directory / 'trajectory.json', archive)

    def _exclusion_reason(self, step, require_verified, outcome):
        review = step['review']
        if step['sensitive']:
            return 'sensitive_content'
        # Trusted controller may know that a coordinate action cannot be
        # represented faithfully by the decision schema (e.g. a slider value).
        # Human keep/annotation never overrides this execution-side evidence.
        controller_exclusion = step.get('metadata', {}).get('sft_exclude_reason')
        if controller_exclusion:
            return str(controller_exclusion)
        if review.get('status') != 'reviewed' or not review.get('keep'):
            return 'not_approved_by_human'
        if not review.get('safe_for_training'):
            return 'excluded_from_training'
        if outcome['verification'].get('status') == 'failed':
            return 'independent_task_verification_failed'
        if require_verified and not outcome['independent_verified']:
            return 'independent_verification_unavailable'
        status = step['execution_info'].get('status', 'unknown')
        if status in _BAD_STATUS or status not in _GOOD_STATUS:
            return 'execution_not_confirmed'
        if step['action'] is None:
            return step['semantic_error'] or 'invalid_action'
        if not step['policy_input']:
            return 'missing_pre_action_input'
        action, args = step['action']['action'], step['action']['args']
        if action in ('switch_tab', 'close_tab') and args.get('tab_id') is not None:
            if args['tab_id'] not in step['prompt_metadata'].get('exposed_tab_ids', []):
                return 'unexposed_tab_id'
        if action == 'handle_dialog' and args.get('dialog_id') is not None:
            if args['dialog_id'] not in step['prompt_metadata'].get('exposed_dialog_ids', []):
                return 'unexposed_dialog_id'
        if step['observation'].get('info', {}).get('screenshot_stale'):
            return 'stale_pre_action_image'
        if step['action']['action'] in ('request_finish', 'fail'):
            return 'terminal_human_claim_is_not_demonstration'
        return None

    def export_sft(self, path=None, *, require_verified=False):
        """Export reviewed decision demonstrations; images are portable relative paths."""
        self._assert_open()
        if type(require_verified) is not bool:
            raise ValueError('require_verified must be a boolean')
        if not self._stopped:
            raise ValueError('Stop recording before exporting reviewed demonstrations')
        outcome = self._outcome_record()
        destination = (Path(path).resolve() if path else
                       self.directory / 'exports' / uuid.uuid4().hex / 'decision_sft.jsonl')
        if destination.exists():
            raise FileExistsError(destination)
        records, excluded, assets = [], [], set()
        for step in self.steps:
            reason = self._exclusion_reason(step, require_verified, outcome)
            if reason:
                excluded.append({'step_id': step['step_id'], 'reason': reason})
                continue
            action = step['action']
            completion = {'next_action': {'type': action['action'], **copy.deepcopy(action['args'])}}
            effect = step['review'].get('expected_effect')
            if effect:
                completion['expected_effect'] = effect
            raw = json.dumps(completion, ensure_ascii=False)
            decoded = action_from_decision(parse_decision(raw))
            if decoded is None or validate_action(decoded):
                raise ValueError('Human supervision no longer matches the decision action schema')
            prompt = copy.deepcopy(step['policy_input'])
            images = []
            for message in prompt:
                if not isinstance(message.get('content'), list):
                    continue
                content = []
                for part in message['content']:
                    if part.get('type') != 'image_url':
                        content.append(part)
                        continue
                    relative = part['image_url']['url']
                    source = (self.directory / relative).resolve()
                    if (not relative.startswith('assets/') or not source.is_relative_to(self.directory)
                            or not source.is_file()):
                        raise ValueError('Missing or unsafe pre-action image asset')
                    target_image = destination.parent / 'assets' / source.name
                    target_image.parent.mkdir(parents=True, exist_ok=True)
                    if not target_image.exists() and target_image.resolve() != source:
                        shutil.copyfile(source, target_image)
                    relative_image = 'assets/' + source.name
                    images.append(relative_image)
                    assets.add(relative_image)
                    content.append({'type': 'image'})
                message['content'] = content
            records.append({'prompt': prompt, 'completion': [{'role': 'assistant', 'content': raw}],
                'images': images, 'image_root': '.', 'format': 'messages_with_relative_image_paths',
                'actor_role': 'decision', 'actor': 'human', 'source': 'human',
                'task_id': self.session_id, 'step': step['step'], 'action_id': step['step_id'],
                'event_id': step['event_id'], 'observation_id': step['observation']['id'],
                'human_reviewed': True, 'independent_verified': outcome['independent_verified'],
                'reward': outcome['reward'], 'reward_valid': outcome['reward_valid'],
                'verification_source': 'completion_contract' if outcome['independent_verified'] else 'human_review_only',
                'human_annotations': {key: step['review'].get(key) for key in ('intent', 'expected_effect')},
                'provenance': {'actor': 'human', 'policy': 'external', 'on_policy': False},
                'loss_scope': 'completion_only', 'schema_version': TRAJECTORY_VERSION})
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open('x', encoding='utf-8') as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        self.writer.emit('human_sft_export', actor_role='human', source='human', count=len(records),
                         path=str(destination), excluded=excluded,
                         independent_verified=outcome['independent_verified'])
        self._save_archive()
        return {'path': str(destination), 'count': len(records), 'excluded': excluded,
                'assets': sorted(assets), 'independent_verified': outcome['independent_verified'],
                'human_reviewed': True, 'reward_valid': outcome['reward_valid']}

    def close(self):
        if not self._closed:
            self.writer.close()
            self._prepared.clear()
            self._closed = True
