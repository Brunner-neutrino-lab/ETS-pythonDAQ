# config.py is the per-machine experiment config (gitignored — see .gitignore).
# On a fresh checkout it won't exist, so seed it from the tracked template
# before anything tries to import it.  Existing config.py is left untouched.
import os as _os, shutil as _shutil
_here = _os.path.dirname(_os.path.abspath(__file__))
if not _os.path.exists(_os.path.join(_here, "config.py")):
    _tmpl = _os.path.join(_here, "config.example.py")
    if _os.path.exists(_tmpl):
        _shutil.copyfile(_tmpl, _os.path.join(_here, "config.py"))

from .digitizer  import make_digitizer, DigitizerResult
from .config     import ExperimentConfig
from .resume     import RunManifest
from .storage    import RunFile
from .primitives import (move_stage, select_channel, set_bias, bias_off,
                          measure_current, iv_sweep, acquire_pulses, read_flux,
                          read_temperature)

__all__ = [
    "make_digitizer", "DigitizerResult",
    "ExperimentConfig",
    "RunManifest",
    "RunFile",
    "move_stage", "select_channel", "set_bias", "bias_off",
    "measure_current", "iv_sweep", "acquire_pulses", "read_flux",
    "read_temperature",
]
