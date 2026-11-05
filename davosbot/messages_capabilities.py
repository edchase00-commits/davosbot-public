"""Read fixed Messages bundle metadata only; never contact Messages or chat.db.

A scripting declaration is not evidence that the operation works. There is no
send/create implementation, process execution, or caller-controlled file input.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import plistlib
import re
import stat
import sys
from typing import Literal, TypedDict
import xml.etree.ElementTree as ET


_BUNDLES = (
    ("system", Path("/System/Applications/Messages.app")),
    ("applications", Path("/Applications/Messages.app")),
)
_MAX_RESOURCE_ENTRIES = 4096
_MAX_CANDIDATES = 8
_SAFE_BASENAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 _.-]{0,78}[A-Za-z0-9])?\Z", re.ASCII)
_MAX_PLIST_BYTES = 65_536
_MAX_DICTIONARY_BYTES = 262_144
_FLAGS = (
    "send_command_declared", "make_command_declared", "chat_class_declared",
    "application_chat_element_declared", "chat_make_response_declared",
)
_VERSION = re.compile(r"[0-9]{1,8}(?:\.[0-9]{1,8}){0,6}(?:[a-z][0-9]{1,8})?\Z", re.ASCII)
# Strip only this known inert declaration; never load a DTD or another document.
_SDEF_DOCTYPE = re.compile(
    r'<!DOCTYPE\s+dictionary\s+SYSTEM\s+(?P<quote>[\"\'])'
    r'file://(?:localhost)?/System/Library/DTDs/sdef\.dtd(?P=quote)\s*>'
)


ProbeReason = Literal[
    "unsupported_platform", "bundle_not_found", "file_bounds", "bundle_unreadable",
    "invalid_bundle_metadata", "unrecognized_bundle", "unsupported_definition_location",
    "dictionary_not_found", "dictionary_unreadable", "unsupported_dictionary",
    "invalid_dictionary", "declarations_only",
]


class DeclaredCapabilities(TypedDict):
    send_command_declared: bool | None
    make_command_declared: bool | None
    chat_class_declared: bool | None
    application_chat_element_declared: bool | None
    chat_make_response_declared: bool | None


class MessagesCapabilityEvidence(TypedDict):
    schema_version: int
    source: Literal["fixed_bundle_files"]
    probe_state: Literal["observed", "unknown"]
    reason: ProbeReason
    bundle_location: Literal["system", "applications"] | None
    metadata_shape: Literal["unknown", "dictionary", "other"]
    bundle_identity: Literal["unknown", "missing", "legacy_ichat", "mobile_sms", "other"]
    definition_metadata_shape: Literal["unknown", "missing", "string", "array", "dictionary", "boolean", "integer", "real", "data", "date", "other"]
    definition_name_state: Literal["not_attempted", "not_string", "empty", "safe_basename", "unsafe_name"]
    definition_basename: str | None
    definition_source: Literal["none", "declared"]
    resources_scan_status: Literal["not_attempted", "complete", "unreadable", "entry_limit", "candidate_limit"]
    unsafe_candidate_names_present: bool
    sdef_candidates: list[dict]
    app_version: str | None
    app_build: str | None
    declarations: DeclaredCapabilities
    group_creation_works: Literal["unknown"]
    sending_works: Literal["unknown"]


class _Unknown(ValueError):
    def __init__(self, reason: ProbeReason):
        super().__init__(reason)
        self.reason = reason


def _open_fixed_directory(path: Path) -> int:
    """Open a fixed internal absolute directory without following any symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise _Unknown("unsupported_definition_location")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_at(directory: int, name: str, maximum: int) -> bytes:
    """Read one regular file relative to an already pinned bundle directory."""
    if not _safe_basename(name):
        raise _Unknown("unsupported_definition_location")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=directory)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise _Unknown("file_bounds")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise _Unknown("file_bounds")
        return data
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_basename(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 80 and ".." not in value and bool(_SAFE_BASENAME.fullmatch(value))


def _safe_sdef(value: object) -> bool:
    return _safe_basename(value) and value.lower().endswith(".sdef")


def _definition_metadata(info: dict) -> tuple[str, str, str | None]:
    if "OSAScriptingDefinition" not in info:
        return "missing", "not_string", None
    value = info["OSAScriptingDefinition"]
    if isinstance(value, str):
        if not value:
            return "string", "empty", None
        if _safe_basename(value):
            return "string", "safe_basename", value
        return "string", "unsafe_name", None
    for kind, label in ((bool, "boolean"), (int, "integer"), (float, "real"),
                        (list, "array"), (dict, "dictionary"), (bytes, "data"), (datetime, "date")):
        if isinstance(value, kind):
            return label, "not_string", None
    return "other", "not_string", None


def _scan_definition_names(resources: int) -> tuple[str, list[str], bool]:
    """List only bounded safe .sdef basenames directly in this fixed directory."""
    names, unsafe = [], False
    try:
        with os.scandir(resources) as entries:
            for count, entry in enumerate(entries, start=1):
                if count > _MAX_RESOURCE_ENTRIES:
                    return "entry_limit", [], unsafe
                if not entry.name.lower().endswith(".sdef"):
                    continue
                if not _safe_sdef(entry.name):
                    unsafe = True
                    continue
                names.append(entry.name)
                if len(names) > _MAX_CANDIDATES:
                    return "candidate_limit", [], unsafe
        return "complete", sorted(names), unsafe
    except (OSError, _Unknown):
        return "unreadable", [], False


def _inspect_definition(resources: int, name: str, source: str) -> dict:
    # This check also protects callers of the internal helper. Never normalize
    # separators, resolve metadata paths or expand environment/home variables.
    if not _safe_sdef(name):
        raise _Unknown("unsupported_definition_location")
    result = {"basename": name, "source": source, "state": "unknown", "reason": "dictionary_not_found",
              "declarations": {flag: None for flag in _FLAGS}}
    try:
        result["declarations"] = _parse_dictionary(_read_at(resources, name, _MAX_DICTIONARY_BYTES))
    except FileNotFoundError:
        return result
    except OSError:
        result["reason"] = "dictionary_unreadable"
        return result
    except _Unknown as exc:
        result["reason"] = exc.reason
        return result
    result["state"], result["reason"] = "observed", "declarations_only"
    return result


def _parse_dictionary(data: bytes) -> DeclaredCapabilities:
    if len(data) > _MAX_DICTIONARY_BYTES:
        raise _Unknown("file_bounds")
    try:
        text = data.decode("utf-8-sig")
        text = _SDEF_DOCTYPE.sub("", text)
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise _Unknown("unsupported_dictionary")
        root = ET.fromstring(text)
    except (UnicodeError, ET.ParseError):
        raise _Unknown("invalid_dictionary") from None
    if root.tag != "dictionary":
        raise _Unknown("invalid_dictionary")
    # External includes and namespaced schemas are not resolved or interpreted.
    # Absence from an incomplete dictionary cannot produce negative assertions.
    if any(not isinstance(node.tag, str) or "{" in node.tag or node.tag == "include"
           for node in root.iter()):
        raise _Unknown("unsupported_dictionary")
    suites = root.findall("suite")
    if not suites:
        raise _Unknown("invalid_dictionary")
    commands = {node.get("name") for suite in suites for node in suite.findall("command")}
    classes = [node for suite in suites for node in suite.findall("class")]
    applications = [node for node in classes if node.get("name") == "application"]
    applications += [node for suite in suites for node in suite.findall("class-extension")
                     if node.get("extends") == "application"]
    chats = [node for node in classes if node.get("name") == "chat"]
    chats += [node for suite in suites for node in suite.findall("class-extension")
              if node.get("extends") == "chat"]
    return {
        "send_command_declared": "send" in commands,
        "make_command_declared": "make" in commands,
        "chat_class_declared": any(node.get("name") == "chat" for node in classes),
        "application_chat_element_declared": any(
            node.get("type") == "chat" for app in applications for node in app.findall("element")),
        "chat_make_response_declared": any(
            node.get("command") == "make" for chat in chats for node in chat.findall("responds-to")),
    }


def _version(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 40 and _VERSION.fullmatch(value) else None


def _probe_contents(contents: int, result: MessagesCapabilityEvidence) -> MessagesCapabilityEvidence:
    try:
        raw = _read_at(contents, "Info.plist", _MAX_PLIST_BYTES)
    except _Unknown as exc:
        result["reason"] = exc.reason
        return result
    except OSError:
        result["reason"] = "bundle_unreadable"
        return result
    try:
        info = plistlib.loads(raw)
    except Exception:
        result["reason"] = "invalid_bundle_metadata"
        return result
    result["metadata_shape"] = "dictionary" if isinstance(info, dict) else "other"
    if isinstance(info, dict):
        identity = info.get("CFBundleIdentifier")
        if identity is None:
            result["bundle_identity"] = "missing"
        elif identity == "com.apple.iChat":
            result["bundle_identity"] = "legacy_ichat"
        elif identity == "com.apple.MobileSMS":
            result["bundle_identity"] = "mobile_sms"
        else:
            result["bundle_identity"] = "other"
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") not in ("com.apple.iChat", "com.apple.MobileSMS"):
        result["reason"] = "unrecognized_bundle"
        return result
    result["app_version"] = _version(info.get("CFBundleShortVersionString"))
    result["app_build"] = _version(info.get("CFBundleVersion"))
    shape, name_state, definition = _definition_metadata(info)
    result["definition_metadata_shape"], result["definition_basename"] = shape, definition
    result["definition_name_state"] = name_state
    declared = _safe_sdef(definition)
    result["definition_source"] = "declared" if declared else "none"
    # Pin Resources relative to the same Contents instance used for Info.plist.
    # Neither metadata nor candidate names can supply another directory.
    try:
        resources = os.open("Resources", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY, dir_fd=contents)
    except OSError:
        result["resources_scan_status"] = "unreadable"
        result["reason"] = "dictionary_unreadable" if declared else "unsupported_definition_location"
        return result
    try:
        scan, names, unsafe = _scan_definition_names(resources)
        result["resources_scan_status"], result["unsafe_candidate_names_present"] = scan, unsafe
        # The declared file has priority within the same eight-attempt budget.
        selected = ([definition] if declared else []) + [name for name in names if name != definition]
        if len(selected) > _MAX_CANDIDATES:
            result["resources_scan_status"] = "candidate_limit"
        result["sdef_candidates"] = [
            _inspect_definition(resources, name, "declared" if name == definition else "candidate")
            for name in selected[:_MAX_CANDIDATES]
        ]
    finally:
        os.close(resources)
    if not declared:
        result["reason"] = "unsupported_definition_location"
        return result
    # Undeclared candidate flags never replace/merge into the app's declared
    # dictionary evidence, even when only one plausible candidate exists.
    observed = result["sdef_candidates"][0]
    result["declarations"] = observed["declarations"]
    result["probe_state"], result["reason"] = observed["state"], observed["reason"]
    return result


def probe_messages_capabilities() -> MessagesCapabilityEvidence:
    """Observe metadata and bounded direct Resources dictionaries, never execute."""
    result: MessagesCapabilityEvidence = {
        "schema_version": 2, "source": "fixed_bundle_files", "probe_state": "unknown",
        "reason": "unsupported_platform", "bundle_location": None,
        "metadata_shape": "unknown", "bundle_identity": "unknown",
        "definition_metadata_shape": "unknown", "definition_basename": None,
        "definition_name_state": "not_attempted",
        "definition_source": "none", "resources_scan_status": "not_attempted",
        "unsafe_candidate_names_present": False, "sdef_candidates": [],
        "app_version": None, "app_build": None,
        "declarations": {
            "send_command_declared": None, "make_command_declared": None,
            "chat_class_declared": None, "application_chat_element_declared": None,
            "chat_make_response_declared": None,
        },
        "group_creation_works": "unknown", "sending_works": "unknown",
    }
    if sys.platform != "darwin":
        return result
    result["reason"] = "bundle_not_found"
    for location, bundle in _BUNDLES:
        try:
            bundle_fd = _open_fixed_directory(bundle)
        except FileNotFoundError:
            continue
        except (OSError, _Unknown):
            result["reason"] = "bundle_unreadable"
            return result
        # Do not fall through once the selected bundle itself exists.
        result["bundle_location"] = location
        try:
            contents = os.open("Contents", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_DIRECTORY, dir_fd=bundle_fd)
        except OSError:
            result["reason"] = "bundle_unreadable"
            return result
        finally:
            os.close(bundle_fd)
        try:
            return _probe_contents(contents, result)
        finally:
            os.close(contents)
    return result
