"""Compatibility imports for daemon-owned skill attachment admission."""

from theater.daemon.skill_attachments import Attachment, attach_skills, reject_skills_resume

__all__ = ["Attachment", "attach_skills", "reject_skills_resume"]
