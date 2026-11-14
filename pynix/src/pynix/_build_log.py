"""The messages of a build, read for the cause of each failed derivation."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import huggorm
from huggorm import ActivityType, LogRecord
from huggorm.fod import FodHashMismatch

from pynix._util import filter_log_escapes, log_text, record_message

#: Nix logs the cause of a failed dependency, such as a hash mismatch, as an error.
CAUSE_LEVEL = int(huggorm.Verbosity.WARN)


@dataclass
class BuildLog:
    """The messages of a build, which hold the cause of a failed dependency.

    A callback on the process stream, and not a capture: a capture holds a
    fixed number of messages and results together, so a long build log fills
    it, and the mismatch that comes last is dropped. This keeps the messages
    alone, so no amount of build progress pushes one out.
    """

    messages: list[str] = field(default_factory=list)
    #: Each derivation that Nix built resolved, by the path of the resolved
    #: copy. Nix names the copy in a failure, and the copy is in no closure of
    #: the target. Only the resolution activity carries two fields
    #: (`derivation-resolution-goal.cc:138`): the original and the copy.
    originals: dict[str, str] = field(default_factory=dict)

    def keep(self, record: LogRecord) -> None:
        if record.action() == "msg":
            self.messages.append(filter_log_escapes(record_message(record)))
        elif record.action() == "start" and record.type() == ActivityType.BUILD_WAITING:
            match [log_text(entry) for entry in record.fields() if not entry.is_int()]:
                case [original, resolved]:
                    self.originals[resolved] = original
                case _:
                    pass

    def attributed(self, mismatch: FodHashMismatch) -> FodHashMismatch:
        """*mismatch*, named by the derivation the target holds."""
        if mismatch.drv_path is None or mismatch.drv_path not in self.originals:
            return mismatch
        return dataclasses.replace(mismatch, drv_path=self.originals[mismatch.drv_path])
