"""Validated, immutable snapshots for ComputerCraft update mirrors.

The service deliberately accepts only public GitHub and GitLab repositories for
now.  That keeps fetching separate from the dashboard and avoids treating a
user-provided URL as a general network target.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from _authority import read_json_object, write_json_object

_SCHEMA_VERSION: Final[int] = 1
_MANIFEST_SCHEMA_VERSION: Final[int] = 1
_PROJECT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_GIT_REFERENCE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
_GIT_COMMIT_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$")
_GIT_COMMIT_LINK_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{7,40}$")
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_PART_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_ARCHIVE_BYTES: Final[int] = 32 * 1024 * 1024
_MAX_EXTRACTED_BYTES: Final[int] = 128 * 1024 * 1024
_MAX_FILE_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_FILE_COUNT: Final[int] = 4_000
_MAX_ARCHIVE_MEMBER_COUNT: Final[int] = 8_000
_COPY_CHUNK_BYTES: Final[int] = 64 * 1024
_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_HTTP_USER_AGENT: Final[str] = "YukibotMirror/1.0"
_MAX_BRANCH_REFERENCE_OPTIONS: Final[int] = 100
_MAX_COMMIT_REFERENCE_OPTIONS: Final[int] = 50
_MAX_RETAINED_SNAPSHOT_REVISIONS: Final[int] = 4
_AUTO_SYNC_INTERVAL: Final[timedelta] = timedelta(days=1)
_AUTO_SYNC_INTERVAL_SECONDS: Final[int] = int(_AUTO_SYNC_INTERVAL.total_seconds())
COMPUTERCRAFT_MIRROR_STATE_ROOT: Final[str] = "/.yukibot_mirrors"
COMPUTERCRAFT_MIRROR_STARTUP_DISPATCHER_PATH: Final[str] = f"{COMPUTERCRAFT_MIRROR_STATE_ROOT}/_startup.lua"

COMPUTERCRAFT_MIRROR_INSTALLER: Final[str] = (
    r'''-- Yukibot ComputerCraft mirror installer and updater.
-- Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]

local arguments = {...}
local project_id = arguments[1]
local mirror_url = arguments[2]
local install_root = nil
local quiet = false
local enable_startup = false

for index = 3, #arguments do
  local argument = arguments[index]
  if argument == "--quiet" then
    quiet = true
  elseif argument == "--enable-startup" then
    enable_startup = true
  elseif not install_root then
    install_root = argument
  else
    print("Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]")
    return false
  end
end
install_root = install_root or "/"

local function report(message)
  if not quiet then
    print(message)
  end
end

local function fail(message)
  report("Mirror update failed: " .. message)
  return false
end

if type(project_id) ~= "string" or not project_id:match("^[a-z][a-z0-9%-]*$") then
  print("Usage: wget run <installer-url> <project-id> <project-url> [install-directory] [--enable-startup] [--quiet]")
  return false
end
if type(mirror_url) ~= "string" or not mirror_url:match("^https?://") then
  return fail("project URL must use HTTP or HTTPS")
end
if not http then
  return fail("HTTP is disabled on this computer")
end

mirror_url = mirror_url:gsub("/+$", "")
install_root = install_root:gsub("/+$", "")
if install_root == "" then
  install_root = "/"
elseif install_root:sub(1, 1) ~= "/" then
  install_root = "/" .. install_root
end
if enable_startup and install_root == "/" then
  return fail("automatic boot mode requires an install directory other than /")
end

local api_root = mirror_url:match("^(https?://.-/mirror/v1)/projects/[^/]+$")
if not api_root then
  return fail("project URL must end with /mirror/v1/projects/<project-id>")
end

local function valid_file_path(path)
  if type(path) ~= "string" or path == "" or path:find("\\", 1, true) or path:find("//", 1, true) then
    return false
  end
  for part in path:gmatch("[^/]+") do
    if part == "." or part == ".." then
      return false
    end
  end
  return true
end

local function encode_path(path)
  return (path:gsub("[^%w%-%._~/]", function(character)
    return string.format("%%%02X", string.byte(character))
  end))
end

local function read_json(path)
  if not fs.exists(path) or fs.isDir(path) then
    return nil
  end
  local handle = fs.open(path, "r")
  if not handle then
    return nil
  end
  local raw = handle.readAll()
  handle.close()
  local ok, value = pcall(textutils.unserializeJSON, raw)
  return ok and type(value) == "table" and value or nil
end

local function remove_temporary_file(path)
  if not fs.exists(path) then
    return true
  end
  if fs.isDir(path) then
    return false, "temporary path is a directory: " .. path
  end
  fs.delete(path)
  return true
end

local function replace_temporary_file(temporary_path, target_path)
  local backup_path = target_path .. ".yukibot-mirror.previous"
  local backup_removed, backup_problem = remove_temporary_file(backup_path)
  if not backup_removed then
    return false, backup_problem
  end
  if fs.exists(target_path) then
    if fs.isDir(target_path) then
      return false, "target is a directory: " .. target_path
    end
    fs.move(target_path, backup_path)
  end
  local ok, problem = pcall(fs.move, temporary_path, target_path)
  if not ok then
    if fs.exists(backup_path) and not fs.exists(target_path) then
      fs.move(backup_path, target_path)
    end
    return false, tostring(problem)
  end
  if fs.exists(backup_path) then
    local removed, problem = remove_temporary_file(backup_path)
    if not removed then
      return false, problem
    end
  end
  return true
end

local function write_text_atomic(path, contents)
  local directory = fs.getDir(path)
  if directory ~= "" and not fs.exists(directory) then
    fs.makeDir(directory)
  end
  local temporary_path = path .. ".yukibot-mirror.next"
  local removed, problem = remove_temporary_file(temporary_path)
  if not removed then
    return false, problem
  end
  local handle = fs.open(temporary_path, "w")
  if not handle then
    return false, "could not open " .. temporary_path
  end
  handle.write(contents)
  handle.close()
  return replace_temporary_file(temporary_path, path)
end

local function response_code(response)
  return response and response.getResponseCode and response.getResponseCode() or 0
end

local function fetch_manifest()
  local response, message, failed_response = http.get(mirror_url .. "/manifest.json", { ["Cache-Control"] = "no-cache" })
  if not response then
    local code = response_code(failed_response)
    if failed_response then failed_response.close() end
    return nil, code == 0 and (message or "could not fetch manifest") or ("HTTP " .. code)
  end
  local raw = response.readAll()
  response.close()
  local ok, manifest = pcall(textutils.unserializeJSON, raw)
  if not ok or type(manifest) ~= "table" then
    return nil, "manifest is not valid JSON"
  end
  if manifest.schema ~= 1 or manifest.project ~= project_id or type(manifest.revision) ~= "string" or type(manifest.files) ~= "table" then
    return nil, "manifest has an unsupported format"
  end
  return manifest
end

local function download_file(entry, revision)
  local target_path = fs.combine(install_root, entry.path)
  local directory = fs.getDir(target_path)
  if directory ~= "" and not fs.exists(directory) then
    fs.makeDir(directory)
  end
  local temporary_path = target_path .. ".yukibot-mirror.next"
  local removed, problem = remove_temporary_file(temporary_path)
  if not removed then
    return false, problem
  end
  local response, message, failed_response = http.get(
    mirror_url .. "/files/" .. encode_path(entry.path) .. "?revision=" .. revision,
    { ["Cache-Control"] = "no-cache" }
  )
  if not response then
    local code = response_code(failed_response)
    if failed_response then failed_response.close() end
    if code == 409 then
      return false, "snapshot_changed"
    end
    return false, code == 0 and (message or "could not download " .. entry.path) or ("HTTP " .. code)
  end
  local handle = fs.open(temporary_path, "w")
  if not handle then
    response.close()
    return false, "could not write " .. temporary_path
  end
  local size = 0
  while true do
    local chunk = response.read(8192)
    if not chunk then break end
    size = size + #chunk
    handle.write(chunk)
  end
  handle.close()
  response.close()
  if size ~= entry.size then
    remove_temporary_file(temporary_path)
    return false, "download size did not match manifest for " .. entry.path
  end
  return replace_temporary_file(temporary_path, target_path)
end

local function updater_script(boot_updates_enabled)
  local quote = string.format
  local arguments = quote("%q", "wget") .. ", " .. quote("%q", "run") .. ", "
    .. quote("%q", api_root .. "/installer.lua") .. ", " .. quote("%q", project_id) .. ", "
    .. quote("%q", mirror_url) .. ", " .. quote("%q", install_root) .. ", " .. quote("%q", "--quiet")
  if boot_updates_enabled then
    arguments = arguments .. ", " .. quote("%q", "--enable-startup")
  end
  return "return shell.run(" .. arguments .. ")\n"
end

local function startup_dispatcher_script(state_root)
  local quote = string.format
  return "-- Yukibot mirror boot updater.\n"
    .. "local state_root = " .. quote("%q", state_root) .. "\n"
    .. "if fs.exists(state_root) and fs.isDir(state_root) then\n"
    .. "  for _, name in ipairs(fs.list(state_root)) do\n"
    .. "    if name:match(" .. quote("%q", "^[a-z][a-z0-9%-]*%.lua$") .. ") then\n"
    .. "      pcall(function() shell.run(fs.combine(state_root, name)) end)\n"
    .. "    end\n"
    .. "  end\n"
    .. "end\n"
end

local function system_startup_script(dispatcher_path, program_startup_path)
  local quote = string.format
  local contents = "-- Managed Yukibot mirror boot updater.\n"
    .. "pcall(function() shell.run(" .. quote("%q", dispatcher_path) .. ") end)\n"
  if not program_startup_path then
    return contents
  end
  return contents
    .. "shell.run(" .. quote("%q", program_startup_path) .. ")\n"
end

local function enable_boot_updates(state_root, program_startup_path)
  local startup_path = "/startup.lua"
  local dispatcher_path = fs.combine(state_root, "_startup.lua")
  local contents = system_startup_script(dispatcher_path, program_startup_path)
  local legacy_contents = system_startup_script(dispatcher_path, nil)
  if fs.exists(startup_path) then
    if fs.isDir(startup_path) then
      report("Automatic boot updates were not enabled because /startup.lua is a directory.")
      return true
    end
    local handle = fs.open(startup_path, "r")
    local existing = handle and handle.readAll() or nil
    if handle then handle.close() end
    if existing == contents then
      return true
    end
    if existing == legacy_contents then
      local saved, problem = write_text_atomic(startup_path, contents)
      if not saved then
        return false, problem
      end
      report("Updated the Yukibot-managed startup.lua to launch this mirror after updating.")
      return true
    end
    report("Automatic boot updates were not enabled because /startup.lua already exists. Add the Yukibot updater snippet to it instead.")
    return true
  end
  local saved, problem = write_text_atomic(startup_path, contents)
  if not saved then
    return false, problem
  end
  report("Automatic Yukibot mirror updates will now run at boot.")
  return true
end

local function sync_once()
  local manifest, manifest_problem = fetch_manifest()
  if not manifest then
    return false, manifest_problem
  end
  local state_root = "__YUKIBOT_MIRROR_STATE_ROOT__"
  local state_path = fs.combine(state_root, project_id .. ".json")
  local previous_state = read_json(state_path)
  if previous_state and previous_state.destination ~= install_root then
    return false, "this mirror is already installed at " .. tostring(previous_state.destination)
  end
  local boot_updates_enabled = enable_startup or (previous_state and previous_state.boot_updates_enabled == true) or false
  local previous_files = {}
  if previous_state and type(previous_state.files) == "table" then
    for _, entry in ipairs(previous_state.files) do
      if type(entry) == "table" and valid_file_path(entry.path) then
        previous_files[entry.path] = entry
      end
    end
  end
  local next_files = {}
  local state_files = {}
  for _, entry in ipairs(manifest.files) do
    if type(entry) ~= "table" or not valid_file_path(entry.path) or type(entry.size) ~= "number" or entry.size < 0
      or entry.size % 1 ~= 0 or type(entry.sha256) ~= "string" then
      return false, "manifest contains an invalid file entry"
    end
    if next_files[entry.path] then
      return false, "manifest contains a duplicate file path"
    end
    next_files[entry.path] = entry
    table.insert(state_files, { path = entry.path, size = entry.size, sha256 = entry.sha256 })
  end
  if boot_updates_enabled and not next_files["startup.lua"] then
    return false, "automatic boot mode requires startup.lua at the mirror root"
  end
  for _, entry in ipairs(manifest.files) do
    local previous = previous_files[entry.path]
    local target_path = fs.combine(install_root, entry.path)
    if not previous and fs.exists(target_path) then
      return false, "refusing to replace unmanaged file: " .. target_path
    end
    local already_current = previous and previous.sha256 == entry.sha256 and previous.size == entry.size
      and fs.exists(target_path) and not fs.isDir(target_path) and fs.getSize(target_path) == entry.size
    if not already_current then
      local downloaded, download_problem = download_file(entry, manifest.revision)
      if not downloaded then
        return false, download_problem
      end
    end
  end
  for path in pairs(previous_files) do
    if not next_files[path] then
      local target_path = fs.combine(install_root, path)
      if fs.exists(target_path) and not fs.isDir(target_path) then
        fs.delete(target_path)
      end
    end
  end
  if not fs.exists(state_root) then
    fs.makeDir(state_root)
  end
  local state = {
    schema = 1,
    project = project_id,
    destination = install_root,
    revision = manifest.revision,
    boot_updates_enabled = boot_updates_enabled,
    files = state_files,
  }
  local saved, save_problem = write_text_atomic(state_path, textutils.serializeJSON(state))
  if not saved then
    return false, save_problem
  end
  local updater_path = fs.combine(state_root, project_id .. ".lua")
  local updater_saved, updater_problem = write_text_atomic(updater_path, updater_script(boot_updates_enabled))
  if not updater_saved then
    return false, updater_problem
  end
  local dispatcher_path = fs.combine(state_root, "_startup.lua")
  local dispatcher_saved, dispatcher_problem = write_text_atomic(
    dispatcher_path,
    startup_dispatcher_script(state_root)
  )
  if not dispatcher_saved then
    return false, dispatcher_problem
  end
  if boot_updates_enabled then
    local startup_enabled, startup_problem = enable_boot_updates(
      state_root,
      fs.combine(install_root, "startup.lua")
    )
    if not startup_enabled then
      return false, startup_problem
    end
  end
  report("Mirror " .. project_id .. " is at revision " .. manifest.revision:sub(1, 12))
  return true
end

for attempt = 1, 2 do
  local updated, problem = sync_once()
  if updated then return true end
  if problem ~= "snapshot_changed" then return fail(problem) end
  report("Mirror changed while updating; retrying once.")
end
return fail("mirror changed repeatedly; try again shortly")
'''.lstrip()
    .replace("__YUKIBOT_MIRROR_STATE_ROOT__", COMPUTERCRAFT_MIRROR_STATE_ROOT)
)


class MirrorError(RuntimeError):
    """Raised when a mirror configuration, source, or snapshot is invalid."""


class MirrorRevisionUnavailable(MirrorError):
    """Raised when a requested immutable mirror snapshot is no longer retained."""


class MirrorGitHost(StrEnum):
    GITHUB = "github"
    GITLAB = "gitlab"


class MirrorSourceKind(StrEnum):
    GIT_REPOSITORY = "git_repository"
    UPLOAD_ARCHIVE = "upload_archive"


class MirrorTrackingMode(StrEnum):
    BRANCH = "branch"
    PINNED_COMMIT = "pinned_commit"


class MirrorSyncState(StrEnum):
    UNPUBLISHED = "unpublished"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    DISABLED = "disabled"


class MirrorAutoSyncOutcome(StrEnum):
    """The result of one scheduled branch check."""

    UNCHANGED = "unchanged"
    PUBLISHED = "published"
    FAILED = "failed"


@dataclasses.dataclass(frozen=True, slots=True)
class GitRepositoryLink:
    """A supported public provider URL and any revision encoded in it."""

    host: MirrorGitHost
    repository: str
    tracking_mode: MirrorTrackingMode | None = None
    ref: str | None = None

    def __post_init__(self) -> None:
        repository = _normalise_repository_identifier(self.repository)
        if (self.tracking_mode is None) != (self.ref is None):
            raise MirrorError("Git repository links must include both a reference and tracking mode.")
        ref = self.ref
        if self.tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
            if ref is None or _GIT_COMMIT_LINK_PATTERN.fullmatch(ref.casefold()) is None:
                raise MirrorError("Git commit links must contain a 7 to 40 character hexadecimal commit SHA.")
            ref = ref.casefold()
        elif self.tracking_mode is MirrorTrackingMode.BRANCH:
            if ref is None:
                raise MirrorError("Git branch links must contain a branch name.")
            ref = _normalise_git_reference(ref, tracking_mode=self.tracking_mode)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "ref", ref)


@dataclasses.dataclass(frozen=True, slots=True)
class GitRepositoryInspection:
    """Provider metadata used to prefill the mirror configuration form."""

    source: GitMirrorSource
    display_name: str
    suggested_project_id: str
    default_branch: str

    def __post_init__(self) -> None:
        display_name = self.display_name.strip()
        if not display_name:
            raise MirrorError("Git providers must return a repository display name.")
        object.__setattr__(self, "display_name", display_name[:80])
        object.__setattr__(self, "suggested_project_id", _normalise_project_id(self.suggested_project_id))
        object.__setattr__(
            self,
            "default_branch",
            _normalise_git_reference(self.default_branch, tracking_mode=MirrorTrackingMode.BRANCH),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class GitReferenceOption:
    """A branch or immutable commit returned by a supported Git provider."""

    tracking_mode: MirrorTrackingMode
    ref: str
    summary: str = ""

    def __post_init__(self) -> None:
        ref = _normalise_git_reference(self.ref, tracking_mode=self.tracking_mode)
        summary = " ".join(self.summary.split())[:120]
        object.__setattr__(self, "ref", ref)
        object.__setattr__(self, "summary", summary)

    @property
    def label(self) -> str:
        if self.tracking_mode is MirrorTrackingMode.BRANCH or not self.summary:
            return self.ref
        return f"{self.ref[:12]} · {self.summary}"


@dataclasses.dataclass(frozen=True, slots=True)
class GitMirrorSource:
    host: MirrorGitHost
    repository: str
    tracking_mode: MirrorTrackingMode
    ref: str

    def __post_init__(self) -> None:
        repository = _normalise_repository_identifier(self.repository)
        ref = _normalise_git_reference(self.ref, tracking_mode=self.tracking_mode)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "ref", ref)

    @property
    def kind(self) -> MirrorSourceKind:
        return MirrorSourceKind.GIT_REPOSITORY

    @property
    def web_url(self) -> str:
        return f"https://{_host_domain(self.host)}/{self.repository}"

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "host": self.host.value,
            "repository": self.repository,
            "tracking_mode": self.tracking_mode.value,
            "ref": self.ref,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class UploadArchiveSource:
    archive_sha256: str
    original_filename: str

    def __post_init__(self) -> None:
        archive_sha256 = self.archive_sha256.strip().casefold()
        original_filename = Path(self.original_filename).name.strip()
        if _SHA256_PATTERN.fullmatch(archive_sha256) is None:
            raise MirrorError("Upload archive SHA-256 must be a lowercase 64-character hexadecimal digest.")
        if not original_filename or Path(original_filename).suffix.casefold() != ".zip":
            raise MirrorError("Mirror uploads must be ZIP archives.")
        object.__setattr__(self, "archive_sha256", archive_sha256)
        object.__setattr__(self, "original_filename", original_filename)

    @property
    def kind(self) -> MirrorSourceKind:
        return MirrorSourceKind.UPLOAD_ARCHIVE

    def to_mapping(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "archive_sha256": self.archive_sha256,
            "original_filename": self.original_filename,
        }


MirrorSource = GitMirrorSource | UploadArchiveSource


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorFileRecord:
    path: str
    size: int
    sha256: str

    def to_mapping(self) -> dict[str, object]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorProject:
    project_id: str
    display_name: str
    owner_user_id: int
    source: MirrorSource
    publish_root: str = ""
    sync_state: MirrorSyncState = MirrorSyncState.UNPUBLISHED
    status_detail: str | None = None
    published_revision: str | None = None
    published_at: str | None = None
    last_checked_at: str | None = None
    next_check_at: str | None = None

    def __post_init__(self) -> None:
        project_id = _normalise_project_id(self.project_id)
        display_name = self.display_name.strip()
        if not display_name or len(display_name) > 80:
            raise MirrorError("Mirror display names must contain between 1 and 80 characters.")
        if self.owner_user_id <= 0:
            raise MirrorError("Mirror owners must have a positive Discord user ID.")
        status_detail = _normalise_status_detail(self.status_detail)
        published_revision = _normalise_published_revision(self.published_revision)
        published_at = _normalise_timestamp(self.published_at)
        last_checked_at = _normalise_timestamp(self.last_checked_at)
        next_check_at = _normalise_timestamp(self.next_check_at)
        if (published_revision is None) != (published_at is None):
            raise MirrorError("Published mirror revisions and timestamps must be recorded together.")
        if not self.is_auto_sync_eligible:
            last_checked_at = None
            next_check_at = None
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(self, "publish_root", _normalise_publish_root(self.publish_root))
        object.__setattr__(self, "status_detail", status_detail)
        object.__setattr__(self, "published_revision", published_revision)
        object.__setattr__(self, "published_at", published_at)
        object.__setattr__(self, "last_checked_at", last_checked_at)
        object.__setattr__(self, "next_check_at", next_check_at)

    @property
    def is_published(self) -> bool:
        return self.sync_state is MirrorSyncState.PUBLISHED and self.published_revision is not None

    @property
    def is_snapshot_available(self) -> bool:
        """Whether a validated snapshot remains publicly available."""

        return self.sync_state is not MirrorSyncState.DISABLED and self.published_revision is not None

    @property
    def is_auto_sync_eligible(self) -> bool:
        """Whether Portal should perform a daily source check for this project."""

        return (
            self.sync_state is not MirrorSyncState.DISABLED
            and isinstance(self.source, GitMirrorSource)
            and self.source.tracking_mode is MirrorTrackingMode.BRANCH
        )

    @property
    def source_label(self) -> str:
        if isinstance(self.source, GitMirrorSource):
            return f"{self.source.host.value.title()} · {self.source.repository}"
        return f"Uploaded ZIP · {self.source.original_filename}"

    @property
    def tracking_label(self) -> str:
        if not isinstance(self.source, GitMirrorSource):
            return "Uploaded archive"
        if self.source.tracking_mode is MirrorTrackingMode.BRANCH:
            return f"Tracking {self.source.ref}"
        return f"Pinned {self.source.ref[:12]}"

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "project_id": self.project_id,
            "display_name": self.display_name,
            "owner_user_id": self.owner_user_id,
            "source": self.source.to_mapping(),
            "publish_root": self.publish_root,
            "sync_state": self.sync_state.value,
        }
        if self.status_detail is not None:
            payload["status_detail"] = self.status_detail
        if self.published_revision is not None:
            payload["published_revision"] = self.published_revision
        if self.published_at is not None:
            payload["published_at"] = self.published_at
        if self.last_checked_at is not None:
            payload["last_checked_at"] = self.last_checked_at
        if self.next_check_at is not None:
            payload["next_check_at"] = self.next_check_at
        return payload


@dataclasses.dataclass(frozen=True, slots=True)
class MirrorAutoSyncResult:
    """A completed scheduled source check, suitable for Portal logging."""

    project_id: str
    outcome: MirrorAutoSyncOutcome
    project: MirrorProject


def _host_domain(host: MirrorGitHost) -> str:
    if host is MirrorGitHost.GITHUB:
        return "github.com"
    if host is MirrorGitHost.GITLAB:
        return "gitlab.com"
    raise AssertionError(f"Unhandled Git host: {host}")


def _normalise_project_id(value: str) -> str:
    project_id = value.strip().casefold()
    if _PROJECT_ID_PATTERN.fullmatch(project_id) is None:
        raise MirrorError("Mirror IDs must use lowercase letters, numbers, and hyphens, beginning with a letter.")
    return project_id


def _suggest_project_id(repository: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", repository.rsplit("/", maxsplit=1)[-1].casefold()).strip("-")
    if not slug:
        slug = "mirror"
    if not slug[0].isalpha():
        slug = f"mirror-{slug}"
    return _normalise_project_id(slug[:48].rstrip("-") or "mirror")


def _normalise_repository_identifier(value: str) -> str:
    repository = value.strip().strip("/")
    parts = repository.split("/")
    if len(parts) < 2 or any(_REPOSITORY_PART_PATTERN.fullmatch(part) is None for part in parts):
        raise MirrorError("Repository paths must contain valid owner/group and project segments.")
    return "/".join(parts)


def _normalise_git_reference(value: str, *, tracking_mode: MirrorTrackingMode) -> str:
    reference = value.strip()
    if tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
        reference = reference.casefold()
        if _GIT_COMMIT_PATTERN.fullmatch(reference) is None:
            raise MirrorError("Pinned Git revisions must be complete 40-character commit SHAs.")
        return reference
    if _GIT_REFERENCE_PATTERN.fullmatch(reference) is None or ".." in reference or reference.endswith("."):
        raise MirrorError("Git branch names contain unsupported characters.")
    return reference


def _normalise_publish_root(value: str) -> str:
    root = value.strip().strip("/")
    if not root:
        return ""
    if "\\" in root:
        raise MirrorError("Publish roots must use forward slashes.")
    parts = root.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MirrorError("Publish roots must be a safe relative path.")
    return "/".join(parts)


def _normalise_status_detail(value: str | None) -> str | None:
    if value is None:
        return None
    detail = value.strip()
    return detail[:500] or None


def _normalise_published_revision(value: str | None) -> str | None:
    if value is None:
        return None
    revision = value.strip().casefold()
    if _GIT_COMMIT_PATTERN.fullmatch(revision) is None and _SHA256_PATTERN.fullmatch(revision) is None:
        raise MirrorError("Published revisions must be a Git commit SHA or SHA-256 digest.")
    return revision


def _normalise_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    timestamp = value.strip()
    if not timestamp:
        return None
    try:
        datetime.fromisoformat(timestamp)
    except ValueError as xcp:
        raise MirrorError(f"Invalid mirror timestamp: {timestamp}") from xcp
    return timestamp


def _normalise_utc_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        raise MirrorError("Mirror scheduler timestamps must include a timezone.")
    return value.astimezone(UTC)


def _utc_now_datetime() -> datetime:
    return datetime.now(UTC)


def _timestamp_as_utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _timestamp_from_datetime(value: datetime) -> str:
    return _normalise_utc_datetime(value).isoformat(timespec="seconds")


def parse_public_git_repository_url(value: str) -> tuple[MirrorGitHost, str]:
    """Parse a canonical public GitHub or GitLab repository-root URL."""

    link = parse_public_git_repository_link(value)
    if link.tracking_mode is not None:
        raise MirrorError("Repository URL must point at the project root, not a branch or commit.")
    return (link.host, link.repository)


def parse_public_git_repository_link(value: str) -> GitRepositoryLink:
    """Parse a public provider project, branch, or commit URL without fetching it."""

    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise MirrorError("Repository URLs must be public HTTPS GitHub or GitLab URLs.")
    try:
        port = parsed.port
    except ValueError as xcp:
        raise MirrorError("Repository URLs must use a valid HTTPS authority.") from xcp
    if parsed.query or parsed.fragment or port is not None:
        raise MirrorError("Repository URLs must not include a port, query string, or fragment.")
    hostname = parsed.hostname.casefold() if parsed.hostname is not None else ""
    if hostname == "github.com":
        host = MirrorGitHost.GITHUB
    elif hostname == "gitlab.com":
        host = MirrorGitHost.GITLAB
    else:
        raise MirrorError("Only github.com and gitlab.com repositories are supported.")
    raw_parts = [urllib.parse.unquote(part) for part in parsed.path.strip("/").split("/") if part]
    if any("\x00" in part for part in raw_parts):
        raise MirrorError("Repository URLs contain an invalid path segment.")
    if host is MirrorGitHost.GITHUB:
        return _parse_github_repository_link(raw_parts)
    return _parse_gitlab_repository_link(raw_parts)


def _parse_github_repository_link(parts: list[str]) -> GitRepositoryLink:
    if len(parts) == 2:
        return GitRepositoryLink(host=MirrorGitHost.GITHUB, repository=_repository_from_parts(parts))
    if len(parts) >= 4 and parts[2] == "tree":
        return GitRepositoryLink(
            host=MirrorGitHost.GITHUB,
            repository=_repository_from_parts(parts[:2]),
            tracking_mode=MirrorTrackingMode.BRANCH,
            ref="/".join(parts[3:]),
        )
    if len(parts) == 4 and parts[2] == "commit":
        return GitRepositoryLink(
            host=MirrorGitHost.GITHUB,
            repository=_repository_from_parts(parts[:2]),
            tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
            ref=parts[3],
        )
    raise MirrorError("GitHub URLs must point at a project, a single branch, or a commit.")


def _parse_gitlab_repository_link(parts: list[str]) -> GitRepositoryLink:
    if "-" not in parts:
        return GitRepositoryLink(host=MirrorGitHost.GITLAB, repository=_repository_from_parts(parts))
    separator_index = parts.index("-")
    repository_parts = parts[:separator_index]
    link_parts = parts[separator_index + 1 :]
    if len(link_parts) < 2:
        raise MirrorError("GitLab URLs must point at a project, a single branch, or a commit.")
    link_kind = link_parts[0]
    if link_kind == "tree":
        tracking_mode = MirrorTrackingMode.BRANCH
        ref = "/".join(link_parts[1:])
    elif link_kind == "commit":
        if len(link_parts) != 2:
            raise MirrorError("GitLab commit URLs must point at one commit.")
        tracking_mode = MirrorTrackingMode.PINNED_COMMIT
        ref = link_parts[1]
    else:
        raise MirrorError("GitLab URLs must point at a project, a single branch, or a commit.")
    return GitRepositoryLink(
        host=MirrorGitHost.GITLAB,
        repository=_repository_from_parts(repository_parts),
        tracking_mode=tracking_mode,
        ref=ref,
    )


def _repository_from_parts(parts: list[str]) -> str:
    if parts and parts[-1].endswith(".git"):
        parts = [*parts[:-1], parts[-1][:-4]]
    return _normalise_repository_identifier("/".join(parts))


def _provider_repository_name(metadata: dict[str, object], *, fallback: str) -> str:
    name = metadata.get("name")
    if not isinstance(name, str) or not name.strip():
        return fallback
    return name.strip()


def _provider_default_branch(metadata: dict[str, object]) -> str:
    default_branch = metadata.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise MirrorError("Git repository does not have a default branch to mirror.")
    return _normalise_git_reference(default_branch, tracking_mode=MirrorTrackingMode.BRANCH)


def _provider_git_reference_option(
    *,
    host: MirrorGitHost,
    record: dict[str, object],
    tracking_mode: MirrorTrackingMode,
) -> GitReferenceOption | None:
    if tracking_mode is MirrorTrackingMode.BRANCH:
        reference = record.get("name")
        summary = ""
    elif host is MirrorGitHost.GITHUB:
        reference = record.get("sha")
        commit = record.get("commit")
        summary = commit.get("message", "") if isinstance(commit, dict) else ""
    else:
        reference = record.get("id")
        summary = record.get("title", "")
    if not isinstance(reference, str) or not isinstance(summary, str):
        return None
    try:
        return GitReferenceOption(tracking_mode=tracking_mode, ref=reference, summary=summary)
    except MirrorError:
        return None


class MirrorService:
    """Owns mirror metadata, validated source imports, and public snapshots."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root
        self._index_path = storage_root / "projects.json"
        self._uploads_root = storage_root / "uploads"
        self._snapshots_root = storage_root / "snapshots"
        self._lock = threading.RLock()
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._projects = self._load_projects()
        self._recover_interrupted_syncs()

    def list_projects(self, *, actor_user_id: int, can_manage_all: bool) -> tuple[MirrorProject, ...]:
        with self._lock:
            projects = tuple(
                project
                for project in self._projects.values()
                if can_manage_all or project.owner_user_id == actor_user_id
            )
        return tuple(sorted(projects, key=lambda project: (project.display_name.casefold(), project.project_id)))

    def get_project(self, project_id: str) -> MirrorProject | None:
        normalised_id = _normalise_project_id(project_id)
        with self._lock:
            return self._projects.get(normalised_id)

    def create_git_project(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        repository_url: str,
        ref: str = "master",
        pinned_commit: bool = False,
        publish_root: str = "",
    ) -> MirrorProject:
        host, repository = parse_public_git_repository_url(repository_url)
        tracking_mode = MirrorTrackingMode.PINNED_COMMIT if pinned_commit else MirrorTrackingMode.BRANCH
        return self.create_git_project_from_source(
            project_id=project_id,
            display_name=display_name,
            owner_user_id=owner_user_id,
            source=GitMirrorSource(host=host, repository=repository, tracking_mode=tracking_mode, ref=ref),
            publish_root=publish_root,
        )

    def create_git_project_from_source(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        source: GitMirrorSource,
        publish_root: str = "",
    ) -> MirrorProject:
        project = MirrorProject(
            project_id=project_id,
            display_name=display_name,
            owner_user_id=owner_user_id,
            source=source,
            publish_root=publish_root,
        )
        if project.is_auto_sync_eligible:
            project = dataclasses.replace(
                project,
                next_check_at=_timestamp_from_datetime(
                    self._initial_auto_sync_time(project=project, now=_utc_now_datetime())
                ),
            )
        with self._lock:
            if project.project_id in self._projects:
                raise MirrorError(f"A mirror with ID {project.project_id!r} already exists.")
            self._projects[project.project_id] = project
            self._save_projects_locked()
        return project

    def inspect_git_repository_url(self, repository_url: str) -> GitRepositoryInspection:
        """Fetch metadata and resolve any branch/commit reference in a public provider URL."""

        link = parse_public_git_repository_link(repository_url)
        metadata = self._fetch_git_repository_metadata(host=link.host, repository=link.repository)
        display_name = _provider_repository_name(metadata, fallback=link.repository.rsplit("/", maxsplit=1)[-1])
        default_branch = _provider_default_branch(metadata)
        if link.tracking_mode is MirrorTrackingMode.PINNED_COMMIT:
            if link.ref is None:
                raise MirrorError("Git commit link did not contain a commit SHA.")
            source = GitMirrorSource(
                host=link.host,
                repository=link.repository,
                tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
                ref=self._resolve_git_revision_for_reference(
                    host=link.host,
                    repository=link.repository,
                    ref=link.ref,
                ),
            )
        else:
            branch = default_branch if link.ref is None else link.ref
            self._resolve_git_revision_for_reference(host=link.host, repository=link.repository, ref=branch)
            source = GitMirrorSource(
                host=link.host,
                repository=link.repository,
                tracking_mode=MirrorTrackingMode.BRANCH,
                ref=branch,
            )
        return GitRepositoryInspection(
            source=source,
            display_name=display_name,
            suggested_project_id=_suggest_project_id(link.repository),
            default_branch=default_branch,
        )

    def list_git_reference_options(
        self,
        *,
        host: MirrorGitHost,
        repository: str,
        tracking_mode: MirrorTrackingMode,
    ) -> tuple[GitReferenceOption, ...]:
        """Return a bounded, provider-validated list of selectable Git references."""

        normalised_repository = _normalise_repository_identifier(repository)
        records = self._fetch_git_reference_records(
            host=host,
            repository=normalised_repository,
            tracking_mode=tracking_mode,
        )
        options: list[GitReferenceOption] = []
        seen_references: set[str] = set()
        for record in records:
            option = _provider_git_reference_option(
                host=host,
                record=record,
                tracking_mode=tracking_mode,
            )
            if option is None or option.ref in seen_references:
                continue
            seen_references.add(option.ref)
            options.append(option)
        if not options:
            reference_kind = "branches" if tracking_mode is MirrorTrackingMode.BRANCH else "commits"
            raise MirrorError(f"Git provider did not return any supported {reference_kind}.")
        return tuple(options)

    def create_upload_project(
        self,
        *,
        project_id: str,
        display_name: str,
        owner_user_id: int,
        archive_path: Path,
        publish_root: str = "",
    ) -> MirrorProject:
        if archive_path.suffix.casefold() != ".zip":
            raise MirrorError("Mirror uploads must be ZIP archives.")
        normalised_id = _normalise_project_id(project_id)
        with self._lock:
            if normalised_id in self._projects:
                raise MirrorError(f"A mirror with ID {normalised_id!r} already exists.")
            archive_sha256 = self._copy_upload_archive(project_id=normalised_id, archive_path=archive_path)
            project = MirrorProject(
                project_id=normalised_id,
                display_name=display_name,
                owner_user_id=owner_user_id,
                source=UploadArchiveSource(
                    archive_sha256=archive_sha256,
                    original_filename=archive_path.name,
                ),
                publish_root=publish_root,
            )
            try:
                self._projects[project.project_id] = project
                self._save_projects_locked()
            except Exception:
                self._upload_archive_path(project.project_id).unlink(missing_ok=True)
                raise
        return project

    def refresh_project(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        project_id = _normalise_project_id(project_id)
        refresh_lock = self._refresh_lock(project_id)
        with refresh_lock:
            project = self._require_project_manager(
                project_id=project_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            refreshed, _ = self._refresh_project_locked(project=project, automatic=False)
            return refreshed

    def sync_next_due_git_project(self, *, now: datetime | None = None) -> MirrorAutoSyncResult | None:
        """Check and, if necessary, publish one due branch-tracking Git mirror.

        Portal calls this at a paced interval. The method deliberately chooses at
        most one project, so providers never receive a burst after a restart.
        """

        current_time = _normalise_utc_datetime(now)
        self._assign_initial_auto_sync_times(now=current_time)
        project = self._next_due_auto_sync_project(now=current_time)
        if project is None:
            return None
        with self._refresh_lock(project.project_id):
            current_project = self.get_project(project.project_id)
            if (
                current_project is None
                or not current_project.is_auto_sync_eligible
                or current_project.sync_state is MirrorSyncState.PUBLISHING
                or current_project.next_check_at is None
                or _timestamp_as_utc_datetime(current_project.next_check_at) > current_time
            ):
                return None
            refreshed, outcome = self._refresh_project_locked(project=current_project, automatic=True)
        if outcome is None:
            raise RuntimeError("Scheduled Git mirror refresh did not report an outcome.")
        return MirrorAutoSyncResult(
            project_id=refreshed.project_id,
            outcome=outcome,
            project=refreshed,
        )

    def _refresh_project_locked(
        self,
        *,
        project: MirrorProject,
        automatic: bool,
    ) -> tuple[MirrorProject, MirrorAutoSyncOutcome | None]:
        if project.sync_state is MirrorSyncState.DISABLED:
            raise MirrorError(f"Mirror {project.project_id!r} is disabled.")
        progress_detail = "Checking tracked branch…" if automatic else "Fetching source…"
        self._replace_project(
            dataclasses.replace(
                project,
                sync_state=MirrorSyncState.PUBLISHING,
                status_detail=progress_detail,
            )
        )
        try:
            if automatic:
                source = project.source
                if not isinstance(source, GitMirrorSource) or source.tracking_mode is not MirrorTrackingMode.BRANCH:
                    raise MirrorError("Only branch-tracking Git mirrors can be automatically synced.")
                revision = self._resolve_git_revision(source)
                completed_at = _utc_now_datetime()
                if revision == project.published_revision:
                    last_checked_at, next_check_at = self._scheduled_check_times(project=project, completed_at=completed_at)
                    unchanged_project = dataclasses.replace(
                        project,
                        sync_state=MirrorSyncState.PUBLISHED,
                        status_detail=f"Checked {source.ref}; revision {revision[:12]} is already published.",
                        last_checked_at=last_checked_at,
                        next_check_at=next_check_at,
                    )
                    self._replace_project(unchanged_project)
                    return (unchanged_project, MirrorAutoSyncOutcome.UNCHANGED)
                archive_bytes = self._download_git_archive(source=source, revision=revision)
            else:
                archive_bytes, revision = self._source_archive(project)
            file_count, extracted_bytes = self._publish_archive(
                project=project,
                archive_bytes=archive_bytes,
                revision=revision,
            )
        except Exception as xcp:
            completed_at = _utc_now_datetime()
            last_checked_at, next_check_at = self._scheduled_check_times(project=project, completed_at=completed_at)
            failure_prefix = "Automatic mirror check failed" if automatic else "Mirror refresh failed"
            failed_project = dataclasses.replace(
                project,
                sync_state=MirrorSyncState.FAILED,
                status_detail=_normalise_status_detail(f"{failure_prefix}: {xcp}") or failure_prefix,
                last_checked_at=last_checked_at,
                next_check_at=next_check_at,
            )
            self._replace_project(failed_project)
            if automatic:
                return (failed_project, MirrorAutoSyncOutcome.FAILED)
            if isinstance(xcp, MirrorError):
                raise
            raise MirrorError(f"Mirror refresh failed: {xcp}") from xcp
        completed_at = _utc_now_datetime()
        last_checked_at, next_check_at = self._scheduled_check_times(project=project, completed_at=completed_at)
        detail_prefix = "Automatically published" if automatic else "Published"
        updated_project = dataclasses.replace(
            project,
            sync_state=MirrorSyncState.PUBLISHED,
            status_detail=f"{detail_prefix} {file_count} files ({_format_byte_count(extracted_bytes)}).",
            published_revision=revision,
            published_at=_timestamp_from_datetime(completed_at),
            last_checked_at=last_checked_at,
            next_check_at=next_check_at,
        )
        self._replace_project(updated_project)
        return (
            updated_project,
            MirrorAutoSyncOutcome.PUBLISHED if automatic else None,
        )

    @staticmethod
    def _scheduled_check_times(*, project: MirrorProject, completed_at: datetime) -> tuple[str | None, str | None]:
        if not project.is_auto_sync_eligible:
            return (None, None)
        if project.next_check_at is not None and _timestamp_as_utc_datetime(project.next_check_at) > completed_at:
            next_check_at = project.next_check_at
        else:
            next_check_at = _timestamp_from_datetime(
                MirrorService._initial_auto_sync_time(project=project, now=completed_at)
            )
        return (
            _timestamp_from_datetime(completed_at),
            next_check_at,
        )

    def _assign_initial_auto_sync_times(self, *, now: datetime) -> None:
        with self._lock:
            updated_projects = {
                project_id: dataclasses.replace(
                    project,
                    next_check_at=_timestamp_from_datetime(self._initial_auto_sync_time(project=project, now=now)),
                )
                for project_id, project in self._projects.items()
                if project.is_auto_sync_eligible and project.next_check_at is None
            }
            if not updated_projects:
                return
            self._projects.update(updated_projects)
            self._save_projects_locked()

    @staticmethod
    def _initial_auto_sync_time(*, project: MirrorProject, now: datetime) -> datetime:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        offset_seconds = int.from_bytes(
            hashlib.sha256(project.project_id.encode("utf-8")).digest()[:8],
            byteorder="big",
        ) % _AUTO_SYNC_INTERVAL_SECONDS
        candidate = day_start + timedelta(seconds=offset_seconds)
        return candidate + _AUTO_SYNC_INTERVAL if candidate <= now else candidate

    def _next_due_auto_sync_project(self, *, now: datetime) -> MirrorProject | None:
        with self._lock:
            due_projects = tuple(
                project
                for project in self._projects.values()
                if (
                    project.is_auto_sync_eligible
                    and project.sync_state is not MirrorSyncState.PUBLISHING
                    and project.next_check_at is not None
                    and _timestamp_as_utc_datetime(project.next_check_at) <= now
                )
            )
        return min(
            due_projects,
            key=lambda project: (_timestamp_as_utc_datetime(project.next_check_at or ""), project.project_id),
            default=None,
        )

    def pin_current_revision(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        normalised_id = _normalise_project_id(project_id)
        with self._refresh_lock(normalised_id):
            project = self._require_project_manager(
                project_id=normalised_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            if not isinstance(project.source, GitMirrorSource):
                raise MirrorError("Only Git mirrors can pin a revision.")
            if project.published_revision is None or _GIT_COMMIT_PATTERN.fullmatch(project.published_revision) is None:
                raise MirrorError("Refresh this Git mirror successfully before pinning it.")
            updated_project = dataclasses.replace(
                project,
                source=dataclasses.replace(
                    project.source,
                    tracking_mode=MirrorTrackingMode.PINNED_COMMIT,
                    ref=project.published_revision,
                ),
                status_detail=f"Pinned to {project.published_revision[:12]}.",
            )
            self._replace_project(updated_project)
            return updated_project

    def track_master(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        normalised_id = _normalise_project_id(project_id)
        with self._refresh_lock(normalised_id):
            project = self._require_project_manager(
                project_id=normalised_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            if not isinstance(project.source, GitMirrorSource):
                raise MirrorError("Only Git mirrors can track a branch.")
            updated_project = dataclasses.replace(
                project,
                source=dataclasses.replace(
                    project.source,
                    tracking_mode=MirrorTrackingMode.BRANCH,
                    ref="master",
                ),
                status_detail="Tracking master. Refresh to publish its current commit.",
            )
            self._replace_project(updated_project)
            return updated_project

    def disable_project(self, *, project_id: str, actor_user_id: int, can_manage_all: bool) -> MirrorProject:
        normalised_id = _normalise_project_id(project_id)
        with self._refresh_lock(normalised_id):
            project = self._require_project_manager(
                project_id=normalised_id,
                actor_user_id=actor_user_id,
                can_manage_all=can_manage_all,
            )
            updated_project = dataclasses.replace(
                project,
                sync_state=MirrorSyncState.DISABLED,
                status_detail="Disabled by its owner.",
            )
            self._replace_project(updated_project)
            return updated_project

    def manifest_path(self, project_id: str) -> Path | None:
        project = self.get_project(project_id)
        if project is None or not project.is_snapshot_available or project.published_revision is None:
            return None
        path = self._snapshot_path(project_id=project.project_id, revision=project.published_revision) / "manifest.json"
        return path if path.is_file() and not path.is_symlink() else None

    def file_path(self, *, project_id: str, relative_path: str, revision: str | None = None) -> Path | None:
        project = self.get_project(project_id)
        if project is None or not project.is_snapshot_available or project.published_revision is None:
            return None
        try:
            safe_path = _safe_snapshot_relative_path(relative_path)
        except MirrorError:
            return None
        snapshot_revision = project.published_revision if revision is None else _normalise_published_revision(revision)
        if snapshot_revision is None:
            raise MirrorError("Mirror file revisions must not be empty.")
        snapshot_root = self._snapshot_path(project_id=project.project_id, revision=snapshot_revision)
        if not snapshot_root.is_dir():
            raise MirrorRevisionUnavailable("The requested mirror snapshot is no longer available. Fetch a new manifest.")
        path = snapshot_root / "files" / safe_path
        return path if path.is_file() and not path.is_symlink() else None

    def _refresh_lock(self, project_id: str) -> threading.Lock:
        with self._lock:
            return self._refresh_locks.setdefault(project_id, threading.Lock())

    def _require_project_manager(
        self,
        *,
        project_id: str,
        actor_user_id: int,
        can_manage_all: bool,
    ) -> MirrorProject:
        with self._lock:
            project = self._projects.get(project_id)
        if project is None:
            raise MirrorError(f"Unknown mirror project: {project_id}")
        if not can_manage_all and project.owner_user_id != actor_user_id:
            raise PermissionError("You do not own this mirror project.")
        return project

    def _replace_project(self, project: MirrorProject) -> None:
        with self._lock:
            self._projects[project.project_id] = project
            self._save_projects_locked()

    def _load_projects(self) -> dict[str, MirrorProject]:
        if not self._index_path.exists():
            return {}
        payload = read_json_object(self._index_path)
        schema = payload.get("schema")
        if schema != _SCHEMA_VERSION:
            raise MirrorError(f"Unsupported mirror project schema: {schema!r}")
        raw_projects = payload.get("projects")
        if not isinstance(raw_projects, list):
            raise MirrorError("Mirror project index must contain a projects array.")
        projects: dict[str, MirrorProject] = {}
        for index, raw_project in enumerate(raw_projects):
            project = _project_from_mapping(raw_project, label=f"projects[{index}]")
            if project.project_id in projects:
                raise MirrorError(f"Mirror project index contains duplicate ID: {project.project_id}")
            projects[project.project_id] = project
        return projects

    def _recover_interrupted_syncs(self) -> None:
        """Mark stale publishing states as failed after an unclean Portal shutdown."""

        with self._lock:
            recovered_projects = {
                project_id: dataclasses.replace(
                    project,
                    sync_state=MirrorSyncState.FAILED,
                    status_detail="Previous mirror sync was interrupted; retry the mirror to publish a new snapshot.",
                )
                for project_id, project in self._projects.items()
                if project.sync_state is MirrorSyncState.PUBLISHING
            }
            if not recovered_projects:
                return
            self._projects.update(recovered_projects)
            self._save_projects_locked()

    def _save_projects_locked(self) -> None:
        write_json_object(
            self._index_path,
            {
                "schema": _SCHEMA_VERSION,
                "projects": [
                    project.to_mapping()
                    for project in sorted(self._projects.values(), key=lambda project: project.project_id)
                ],
            },
        )

    def _copy_upload_archive(self, *, project_id: str, archive_path: Path) -> str:
        project_id = _normalise_project_id(project_id)
        try:
            source_size = archive_path.stat().st_size
        except OSError as xcp:
            raise MirrorError(f"Unable to read uploaded archive: {archive_path}") from xcp
        if source_size > _MAX_ARCHIVE_BYTES:
            raise MirrorError(f"Uploaded archives must not exceed {_format_byte_count(_MAX_ARCHIVE_BYTES)}.")
        self._uploads_root.mkdir(parents=True, exist_ok=True)
        destination = self._upload_archive_path(project_id)
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        total_bytes = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._uploads_root,
                prefix=f".{project_id}.",
                suffix=".tmp",
                delete=False,
            ) as target, archive_path.open("rb") as source:
                temporary_path = Path(target.name)
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    total_bytes += len(chunk)
                    if total_bytes > _MAX_ARCHIVE_BYTES:
                        raise MirrorError(f"Uploaded archives must not exceed {_format_byte_count(_MAX_ARCHIVE_BYTES)}.")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            temporary_path.replace(destination)
        except Exception:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
        return digest.hexdigest()

    def _source_archive(self, project: MirrorProject) -> tuple[bytes, str]:
        source = project.source
        if isinstance(source, GitMirrorSource):
            revision = self._resolve_git_revision(source)
            return (self._download_git_archive(source=source, revision=revision), revision)
        archive_path = self._upload_archive_path(project.project_id)
        if not archive_path.is_file():
            raise MirrorError("The uploaded archive is no longer available.")
        archive_bytes = archive_path.read_bytes()
        if len(archive_bytes) > _MAX_ARCHIVE_BYTES:
            raise MirrorError(f"Uploaded archives must not exceed {_format_byte_count(_MAX_ARCHIVE_BYTES)}.")
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if digest != source.archive_sha256:
            raise MirrorError("The uploaded archive no longer matches its recorded SHA-256 digest.")
        return (archive_bytes, digest)

    def _resolve_git_revision(self, source: GitMirrorSource) -> str:
        return self._resolve_git_revision_for_reference(
            host=source.host,
            repository=source.repository,
            ref=source.ref,
        )

    def _fetch_git_repository_metadata(self, *, host: MirrorGitHost, repository: str) -> dict[str, object]:
        if host is MirrorGitHost.GITHUB:
            return self._fetch_json(f"https://api.github.com/repos/{repository}")
        encoded_repository = urllib.parse.quote(repository, safe="")
        return self._fetch_json(f"https://gitlab.com/api/v4/projects/{encoded_repository}")

    def _fetch_git_reference_records(
        self,
        *,
        host: MirrorGitHost,
        repository: str,
        tracking_mode: MirrorTrackingMode,
    ) -> tuple[dict[str, object], ...]:
        if host is MirrorGitHost.GITHUB:
            if tracking_mode is MirrorTrackingMode.BRANCH:
                url = (
                    f"https://api.github.com/repos/{repository}/branches"
                    f"?per_page={_MAX_BRANCH_REFERENCE_OPTIONS}"
                )
            else:
                url = (
                    f"https://api.github.com/repos/{repository}/commits"
                    f"?per_page={_MAX_COMMIT_REFERENCE_OPTIONS}"
                )
        else:
            encoded_repository = urllib.parse.quote(repository, safe="")
            if tracking_mode is MirrorTrackingMode.BRANCH:
                url = (
                    f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/branches"
                    f"?per_page={_MAX_BRANCH_REFERENCE_OPTIONS}"
                )
            else:
                url = (
                    f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/commits"
                    f"?per_page={_MAX_COMMIT_REFERENCE_OPTIONS}"
                )
        return self._fetch_json_list(url)

    def _resolve_git_revision_for_reference(
        self,
        *,
        host: MirrorGitHost,
        repository: str,
        ref: str,
    ) -> str:
        encoded_reference = urllib.parse.quote(ref, safe="")
        if host is MirrorGitHost.GITHUB:
            payload = self._fetch_json(f"https://api.github.com/repos/{repository}/commits/{encoded_reference}")
            revision = payload.get("sha")
        else:
            encoded_repository = urllib.parse.quote(repository, safe="")
            payload = self._fetch_json(
                f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/commits/{encoded_reference}"
            )
            revision = payload.get("id")
        if not isinstance(revision, str) or _GIT_COMMIT_PATTERN.fullmatch(revision.casefold()) is None:
            raise MirrorError("Git provider returned an invalid commit revision.")
        return revision.casefold()

    def _download_git_archive(self, *, source: GitMirrorSource, revision: str) -> bytes:
        if source.host is MirrorGitHost.GITHUB:
            url = f"https://codeload.github.com/{source.repository}/zip/{revision}"
        else:
            encoded_repository = urllib.parse.quote(source.repository, safe="")
            url = (
                f"https://gitlab.com/api/v4/projects/{encoded_repository}/repository/archive.zip"
                f"?sha={urllib.parse.quote(revision, safe='')}"
            )
        return self._download_bytes(url)

    @staticmethod
    def _fetch_json_value(url: str) -> object:
        body = MirrorService._download_bytes(url)
        try:
            return json.loads(body)
        except json.JSONDecodeError as xcp:
            raise MirrorError("Git provider returned invalid JSON.") from xcp

    @staticmethod
    def _fetch_json(url: str) -> dict[str, object]:
        value = MirrorService._fetch_json_value(url)
        if not isinstance(value, dict):
            raise MirrorError("Git provider returned an unexpected response.")
        return {str(key): item for key, item in value.items()}

    @staticmethod
    def _fetch_json_list(url: str) -> tuple[dict[str, object], ...]:
        value = MirrorService._fetch_json_value(url)
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise MirrorError("Git provider returned an unexpected response.")
        return tuple({str(key): item for key, item in record.items()} for record in value)

    @staticmethod
    def _download_bytes(url: str) -> bytes:
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": _HTTP_USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as xcp:
                        raise MirrorError("Git provider returned an invalid Content-Length header.") from xcp
                    if declared_size > _MAX_ARCHIVE_BYTES:
                        raise MirrorError(f"Mirror archives must not exceed {_format_byte_count(_MAX_ARCHIVE_BYTES)}.")
                output = io.BytesIO()
                while chunk := response.read(_COPY_CHUNK_BYTES):
                    if output.tell() + len(chunk) > _MAX_ARCHIVE_BYTES:
                        raise MirrorError(f"Mirror archives must not exceed {_format_byte_count(_MAX_ARCHIVE_BYTES)}.")
                    output.write(chunk)
                return output.getvalue()
        except urllib.error.HTTPError as xcp:
            raise MirrorError(f"Git provider rejected the source ({xcp.code}).") from xcp
        except urllib.error.URLError as xcp:
            raise MirrorError(f"Could not contact Git provider: {xcp.reason}") from xcp

    def _publish_archive(self, *, project: MirrorProject, archive_bytes: bytes, revision: str) -> tuple[int, int]:
        self._snapshots_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f"{project.project_id}-", dir=self._snapshots_root) as temporary_name:
            temporary_root = Path(temporary_name)
            files_root = temporary_root / "files"
            files_root.mkdir()
            file_count, extracted_bytes = self._extract_zip_archive(
                archive_bytes=archive_bytes,
                project=project,
                output_root=files_root,
            )
            records = self._scan_snapshot_files(files_root)
            if len(records) != file_count:
                raise MirrorError("Mirror archive extraction produced an unexpected file count.")
            manifest = {
                "schema": _MANIFEST_SCHEMA_VERSION,
                "project": project.project_id,
                "revision": revision,
                "generated_at": _utc_now(),
                "source": project.source.to_mapping(),
                "publish_root": project.publish_root,
                "files": [record.to_mapping() for record in records],
            }
            (temporary_root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            self._publish_snapshot(project.project_id, revision, temporary_root)
        return (file_count, extracted_bytes)

    def _extract_zip_archive(self, *, archive_bytes: bytes, project: MirrorProject, output_root: Path) -> tuple[int, int]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
        except zipfile.BadZipFile as xcp:
            raise MirrorError("Mirror source is not a valid ZIP archive.") from xcp
        with archive:
            archive_infos = archive.infolist()
            if len(archive_infos) > _MAX_ARCHIVE_MEMBER_COUNT:
                raise MirrorError(f"Mirror source exceeds the {_MAX_ARCHIVE_MEMBER_COUNT} archive-member limit.")
            archive_members = tuple((info, _safe_zip_member_path(info)) for info in archive_infos)
            entries = tuple((info, path) for info, path in archive_members if not info.is_dir())
            if not entries:
                raise MirrorError("Mirror source archive does not contain any files.")
            if len(entries) > _MAX_FILE_COUNT:
                raise MirrorError(f"Mirror source exceeds the {_MAX_FILE_COUNT} file limit.")
            declared_size = sum(info.file_size for info, _ in entries)
            if declared_size > _MAX_EXTRACTED_BYTES:
                raise MirrorError(f"Mirror source exceeds the {_format_byte_count(_MAX_EXTRACTED_BYTES)} extracted-size limit.")
            source_root = self._git_archive_root(entries) if isinstance(project.source, GitMirrorSource) else None
            publish_root = PurePosixPath(project.publish_root) if project.publish_root else None
            output_paths: set[str] = set()
            output_path_keys: set[str] = set()
            file_count = 0
            extracted_bytes = 0
            for info, archive_path in entries:
                source_path = _strip_git_archive_root(archive_path, source_root)
                if source_path is None:
                    continue
                target_relative_path = _path_under_publish_root(source_path, publish_root)
                if target_relative_path is None:
                    continue
                target_relative_text = target_relative_path.as_posix()
                target_casefolded = target_relative_text.casefold()
                if target_relative_text in output_paths or target_casefolded in output_path_keys:
                    raise MirrorError(f"Mirror source contains duplicate output path: {target_relative_text}")
                if info.file_size > _MAX_FILE_BYTES:
                    raise MirrorError(f"Mirror file exceeds the {_format_byte_count(_MAX_FILE_BYTES)} size limit: {target_relative_text}")
                output_paths.add(target_relative_text)
                output_path_keys.add(target_casefolded)
                target_path = output_root / target_relative_path.as_posix()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                written_bytes = 0
                with archive.open(info, "r") as source, target_path.open("xb") as target:
                    while chunk := source.read(_COPY_CHUNK_BYTES):
                        written_bytes += len(chunk)
                        extracted_bytes += len(chunk)
                        if written_bytes > _MAX_FILE_BYTES:
                            raise MirrorError(f"Mirror file exceeds the {_format_byte_count(_MAX_FILE_BYTES)} size limit.")
                        if extracted_bytes > _MAX_EXTRACTED_BYTES:
                            raise MirrorError(
                                f"Mirror source exceeds the {_format_byte_count(_MAX_EXTRACTED_BYTES)} extracted-size limit."
                            )
                        target.write(chunk)
                if written_bytes != info.file_size:
                    raise MirrorError(f"Mirror archive member size changed while extracting: {target_relative_text}")
                file_count += 1
            if file_count == 0:
                root_label = project.publish_root or "/"
                raise MirrorError(f"Publish root does not contain files: {root_label}")
            return (file_count, extracted_bytes)

    @staticmethod
    def _git_archive_root(entries: tuple[tuple[zipfile.ZipInfo, PurePosixPath], ...]) -> str:
        roots = {path.parts[0] for _, path in entries if path.parts}
        if len(roots) != 1:
            raise MirrorError("Git provider archive must contain exactly one top-level source directory.")
        return next(iter(roots))

    @staticmethod
    def _scan_snapshot_files(root: Path) -> tuple[MirrorFileRecord, ...]:
        records: list[MirrorFileRecord] = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            records.append(
                MirrorFileRecord(
                    path=path.relative_to(root).as_posix(),
                    size=path.stat().st_size,
                    sha256=_file_sha256(path),
                )
            )
        return tuple(records)

    def _publish_snapshot(self, project_id: str, revision: str, temporary_root: Path) -> None:
        snapshot_root = self._snapshot_root(project_id)
        snapshot_root.mkdir(parents=True, exist_ok=True)
        revisions_root = self._snapshot_revisions_root(project_id)
        revisions_root.mkdir(exist_ok=True)
        normalised_revision = _normalise_published_revision(revision)
        if normalised_revision is None:
            raise MirrorError("Published snapshots must have a revision.")
        destination = revisions_root / normalised_revision
        next_path = revisions_root / f".{normalised_revision}.next"
        _remove_tree_if_present(next_path)
        os.replace(temporary_root, next_path)
        try:
            if not destination.exists():
                os.replace(next_path, destination)
        finally:
            _remove_tree_if_present(next_path)
        self._prune_snapshot_revisions(project_id, current_revision=normalised_revision)

    def _upload_archive_path(self, project_id: str) -> Path:
        return self._uploads_root / f"{_normalise_project_id(project_id)}.zip"

    def _snapshot_root(self, project_id: str) -> Path:
        return self._snapshots_root / _normalise_project_id(project_id)

    def _snapshot_revisions_root(self, project_id: str) -> Path:
        return self._snapshot_root(project_id) / "revisions"

    def _snapshot_path(self, *, project_id: str, revision: str) -> Path:
        normalised_revision = _normalise_published_revision(revision)
        if normalised_revision is None:
            raise MirrorError("Published snapshots must have a revision.")
        revision_path = self._snapshot_revisions_root(project_id) / normalised_revision
        if revision_path.is_dir():
            return revision_path
        project = self.get_project(project_id)
        if project is not None and project.published_revision == normalised_revision:
            return self._snapshot_root(project_id)
        return revision_path

    def _prune_snapshot_revisions(self, project_id: str, *, current_revision: str) -> None:
        revisions_root = self._snapshot_revisions_root(project_id)
        revisions = tuple(
            path
            for path in revisions_root.iterdir()
            if path.is_dir() and not path.is_symlink() and _normalise_published_revision(path.name) is not None
        )
        current_path = revisions_root / current_revision
        retained_paths = frozenset(
            (current_path,)
            + tuple(
                path
                for path in sorted(revisions, key=lambda path: path.stat().st_mtime_ns, reverse=True)
                if path != current_path
            )[: _MAX_RETAINED_SNAPSHOT_REVISIONS - 1]
        )
        for path in revisions:
            if path not in retained_paths:
                shutil.rmtree(path)


def _project_from_mapping(value: object, *, label: str) -> MirrorProject:
    if not isinstance(value, dict):
        raise MirrorError(f"{label} must be an object.")
    source = _source_from_mapping(value.get("source"), label=f"{label}.source")
    try:
        sync_state = MirrorSyncState(_require_string(value.get("sync_state"), label=f"{label}.sync_state"))
    except ValueError as xcp:
        raise MirrorError(f"{label}.sync_state is invalid.") from xcp
    owner_user_id = value.get("owner_user_id")
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int):
        raise MirrorError(f"{label}.owner_user_id must be an integer.")
    return MirrorProject(
        project_id=_require_string(value.get("project_id"), label=f"{label}.project_id"),
        display_name=_require_string(value.get("display_name"), label=f"{label}.display_name"),
        owner_user_id=owner_user_id,
        source=source,
        publish_root=_optional_string(value.get("publish_root"), label=f"{label}.publish_root") or "",
        sync_state=sync_state,
        status_detail=_optional_string(value.get("status_detail"), label=f"{label}.status_detail"),
        published_revision=_optional_string(value.get("published_revision"), label=f"{label}.published_revision"),
        published_at=_optional_string(value.get("published_at"), label=f"{label}.published_at"),
        last_checked_at=_optional_string(value.get("last_checked_at"), label=f"{label}.last_checked_at"),
        next_check_at=_optional_string(value.get("next_check_at"), label=f"{label}.next_check_at"),
    )


def _source_from_mapping(value: object, *, label: str) -> MirrorSource:
    if not isinstance(value, dict):
        raise MirrorError(f"{label} must be an object.")
    try:
        kind = MirrorSourceKind(_require_string(value.get("kind"), label=f"{label}.kind"))
    except ValueError as xcp:
        raise MirrorError(f"{label}.kind is invalid.") from xcp
    if kind is MirrorSourceKind.GIT_REPOSITORY:
        try:
            host = MirrorGitHost(_require_string(value.get("host"), label=f"{label}.host"))
            tracking_mode = MirrorTrackingMode(
                _require_string(value.get("tracking_mode"), label=f"{label}.tracking_mode")
            )
        except ValueError as xcp:
            raise MirrorError(f"{label} has an invalid Git source setting.") from xcp
        return GitMirrorSource(
            host=host,
            repository=_require_string(value.get("repository"), label=f"{label}.repository"),
            tracking_mode=tracking_mode,
            ref=_require_string(value.get("ref"), label=f"{label}.ref"),
        )
    return UploadArchiveSource(
        archive_sha256=_require_string(value.get("archive_sha256"), label=f"{label}.archive_sha256"),
        original_filename=_require_string(value.get("original_filename"), label=f"{label}.original_filename"),
    )


def _require_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MirrorError(f"{label} must be a non-empty string.")
    return value


def _optional_string(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MirrorError(f"{label} must be a string when provided.")
    return value


def _safe_zip_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    if info.flag_bits & 0x1:
        raise MirrorError(f"Refusing encrypted archive member: {info.filename}")
    if "\\" in info.filename or "\x00" in info.filename:
        raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
    raw_path = info.filename.rstrip("/")
    if not raw_path or raw_path.startswith("/"):
        raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
    parts = raw_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MirrorError(f"Refusing unsafe archive member: {info.filename}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise MirrorError(f"Refusing symlinked archive member: {info.filename}")
    file_type = stat.S_IFMT(mode)
    expected_type = stat.S_IFDIR if info.is_dir() else stat.S_IFREG
    if file_type not in {0, expected_type}:
        raise MirrorError(f"Refusing non-regular archive member: {info.filename}")
    return PurePosixPath(*parts)


def _strip_git_archive_root(path: PurePosixPath, root: str | None) -> PurePosixPath | None:
    if root is None:
        return path
    if not path.parts or path.parts[0] != root:
        raise MirrorError(f"Git provider archive contained an unexpected path: {path.as_posix()}")
    if len(path.parts) == 1:
        return None
    return PurePosixPath(*path.parts[1:])


def _path_under_publish_root(path: PurePosixPath, publish_root: PurePosixPath | None) -> PurePosixPath | None:
    if publish_root is None:
        return path
    prefix_length = len(publish_root.parts)
    if path.parts[:prefix_length] != publish_root.parts:
        return None
    if len(path.parts) == prefix_length:
        return None
    return PurePosixPath(*path.parts[prefix_length:])


def _safe_snapshot_relative_path(value: str) -> str:
    decoded = urllib.parse.unquote(value)
    if "\\" in decoded or "\x00" in decoded:
        raise MirrorError("Mirror file path is invalid.")
    path = PurePosixPath(decoded)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MirrorError("Mirror file path is invalid.")
    return path.as_posix()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_tree_if_present(path: Path) -> None:
    if path.exists():
        if not path.is_dir():
            raise MirrorError(f"Mirror snapshot path is not a directory: {path}")
        shutil.rmtree(path)


def _utc_now() -> str:
    return _timestamp_from_datetime(datetime.now(UTC))


def _format_byte_count(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KiB"
    return f"{value / (1024 * 1024):.1f} MiB"
