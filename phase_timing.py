"""Wall-clock phases include preparation, retries and evidence work, not just HTTP."""
from functools import wraps
import time
import uuid


def timed_phase(phase):
    def decorate(function):
        @wraps(function)
        def measured(self, *args, **kwargs):
            if not hasattr(self, '_run_started'):
                return function(self, *args, **kwargs)
            previous_calls = getattr(self, "planning_calls", [])
            previous_count = len(previous_calls)
            started = time.perf_counter()
            ident = uuid.uuid4().hex[:12]
            step = getattr(self, '_current_step', 0)
            report = {'phase': phase, 'phase_id': ident, 'step': step}
            self._publish_diagnostic('phase_timing', **report, status='running',
                                     started_at=time.time())
            status = 'completed'
            try:
                return function(self, *args, **kwargs)
            except BaseException:
                status = 'failed'
                raise
            finally:
                report.update(status=status, elapsed_ms=round((time.perf_counter()-started)*1000, 2),
                    since_start_ms=round((time.perf_counter()-getattr(self, '_run_started', started))*1000, 2))
                if phase == 'planning':
                    calls = getattr(self, 'planning_calls', [])
                    if calls is previous_calls:
                        calls = calls[previous_count:]
                    report['request_ms'] = round(sum(sum(a.get('latency_ms', 0) for a in c.get('attempts', []))
                        if c.get('attempts') else c.get('latency_ms', 0) for c in calls), 2)
                    report['attempts'] = len(calls)
                getattr(self, '_phase_timings', []).append(dict(report))
                self._publish_diagnostic('phase_timing', **report)
        return measured
    return decorate
