"""Process-local boundaries shared by native handlers, timers and Work adapters.

Never hold these around a whole message handler. Group state and personality
file operations do not acquire the schedule lock; schedule callbacks may read
either. Model calls stay outside file/group locks. These do not coordinate
separate processes or trusted arbitrary shell/SQL tools.
"""

from functools import wraps
from threading import RLock


GROUP_STATE_LOCK = RLock()
PERSONALITY_FILE_LOCK = RLock()
SCHEDULE_LOCK = RLock()
BUDGET_ALERT_LOCK = RLock()
MODEL_STATE_LOCK = RLock()


def _locked(lock):
    def decorate(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            with lock:
                return function(*args, **kwargs)
        return guarded
    return decorate


group_state_locked = _locked(GROUP_STATE_LOCK)
personality_file_locked = _locked(PERSONALITY_FILE_LOCK)
schedule_locked = _locked(SCHEDULE_LOCK)
