from __future__ import annotations

import enum
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

import emoji
import hikari
import lightbulb
import requests
from hikari_ui import (
    Editor,
    EditorButton,
    EditorLayout,
    EditorPageState,
    EditorRequest,
    EditorResponse,
    EditorSelectOption,
    InteractionDeferral,
    ModalKit,
    ModalRequest,
    ModalSchema,
    ModalTextField,
    PagedActionCodec,
)

import config
from _editor_session import startup_editor_prefix
from _security import Access_Control
from cmd_voice_common import (
    DISCORD_CUSTOM_EMOJI_RE,
    EMOJI_TAG_RE,
    MAX_TTS_VOICES,
    USER_MENTION_RE,
    PronunciationFormat,
    PronunciationOverride,
    TextSubstitutionRule,
    VoiceLinkRule,
    VoiceLinkRuleMode,
)
from cmd_voice_service import (
    HFRepoRef,
    PiperPythonVoiceRuntime,
    SpeechContent,
    VoiceJob,
    VoiceRuntimeResetResult,
    VoiceTTSService,
    group_voice,
    log,
)

_VOICE_SETTINGS_COMMAND = "/voice settings"
_VOICE_SETTINGS_EDITOR_PREFIX = "vs:"
_VOICE_SETTINGS_SUBSTITUTION_MODAL_PREFIX = "vss:"
_VOICE_SETTINGS_MENTION_MODAL_PREFIX = "vsm:"
_VOICE_SETTINGS_PRONUNCIATION_MODAL_PREFIX = "vsp:"
_VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID = "source"
_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID = "target"
_VOICE_SETTINGS_ENTRY_CASE_SENSITIVE_FIELD_ID = "case_sensitive"
_VOICE_SETTINGS_MENTION_USER_FIELD_ID = "user"
_VOICE_SETTINGS_PRONUNCIATION_FORMAT_FIELD_ID = "format"
_VOICE_SUBSTITUTION_SOURCE_FIELD_MAX_LENGTH = VoiceTTSService._MAX_SUBSTITUTION_KEY_CHARS
_VOICE_SETTINGS_PAGE_SIZE = 25
_VOICE_SETTINGS_VARIANT_PAGE_SIZE = MAX_TTS_VOICES - 1
_VOICE_SETTINGS_CLEAR_VARIANT_VALUE = "__clear__"
_VOICE_ADMIN_COMMAND = "/voice admin"
_VOICE_ADMIN_EDITOR_PREFIX = "va:"
_VOICE_ADMIN_MODEL_MODAL_PREFIX = "vam:"
_VOICE_ADMIN_SUBSTITUTION_MODAL_PREFIX = "vas:"
_VOICE_ADMIN_MENTION_MODAL_PREFIX = "van:"
_VOICE_ADMIN_ENTRY_MODAL_PREFIX = "vae:"
_VOICE_ADMIN_TOKEN_MODAL_PREFIX = "vat:"
_VOICE_ADMIN_PRONUNCIATION_MODAL_PREFIX = "vap:"
_VOICE_ADMIN_LINK_HOST_MODAL_PREFIX = "vah:"
_VOICE_ADMIN_LINK_RULE_MODAL_PREFIX = "var:"
_VOICE_ADMIN_MODEL_URL_FIELD_ID = "url"
_VOICE_ADMIN_SOURCE_FIELD_ID = "source"
_VOICE_ADMIN_TARGET_FIELD_ID = "target"
_VOICE_ADMIN_CASE_SENSITIVE_FIELD_ID = "case_sensitive"
_VOICE_ADMIN_MENTION_USER_FIELD_ID = "user"
_VOICE_ADMIN_PRONUNCIATION_VOICE_FIELD_ID = "voice"
_VOICE_ADMIN_PRONUNCIATION_FORMAT_FIELD_ID = "format"
_VOICE_ADMIN_HOST_FIELD_ID = "host"
_VOICE_ADMIN_LABEL_FIELD_ID = "label"
_VOICE_ADMIN_LINK_RULE_URL_FIELD_ID = "rule_url"
_VOICE_ADMIN_PATH_REGEX_FIELD_ID = "path_regex"
_VOICE_ADMIN_TEMPLATE_FIELD_ID = "template"
_VOICE_ADMIN_STATE_VALUE_SEPARATOR = "~"
_VOICE_ADMIN_VOICE_CHANNEL_TYPES: tuple[hikari.ChannelType, ...] = (hikari.ChannelType.GUILD_VOICE,)
_VOICE_ADMIN_TTS_CHANNEL_TYPES: tuple[hikari.ChannelType, ...] = (
    hikari.ChannelType.GUILD_TEXT,
    hikari.ChannelType.GUILD_NEWS,
)

ValueT = TypeVar("ValueT")


@dataclass(slots=True, frozen=True)
class NormalizedVoiceSource:
    raw: str
    key: str
    emoji_token: str | None = None

    @property
    def is_emoji(self) -> bool:
        return self.emoji_token is not None

    def display(self) -> str:
        if self.emoji_token is None:
            return f"`{self.key}`"
        return f"`{self.emoji_token}` (`{self.key}`)"


def _normalise_voice_source(source: str) -> NormalizedVoiceSource:
    value = source.strip()
    if not value:
        return NormalizedVoiceSource(raw=source, key="")

    tag: str | None = None
    if match := DISCORD_CUSTOM_EMOJI_RE.fullmatch(value):
        tag = f":{match.group(1).lower()}:"
    elif EMOJI_TAG_RE.fullmatch(value):
        tag = value.lower()
    else:
        demojized = emoji.demojize(value, language="en")
        if demojized != value and EMOJI_TAG_RE.fullmatch(demojized):
            tag = demojized.lower()

    if tag is None:
        return NormalizedVoiceSource(raw=value, key=value)
    return NormalizedVoiceSource(raw=value, key=tag.strip(":"), emoji_token=value)


class VoiceSettingsActionKind(enum.StrEnum):
    SHOW_SECTION = "ss"
    SHOW_OVERVIEW = "so"
    SHOW_VOICE = "sv"
    SHOW_MENTIONS = "sm"
    SHOW_SUBSTITUTIONS = "su"
    SHOW_PRONUNCIATIONS = "sp"
    PAGE = "pg"
    REFRESH = "rf"
    CLOSE = "cl"
    TOGGLE_LISTEN = "tl"
    TOGGLE_AUTOCORRECT = "ta"
    SET_VOICE = "vv"
    SET_VARIANT = "vr"
    SELECT_SUBSTITUTION = "cs"
    ADD_SUBSTITUTION = "as"
    EDIT_SUBSTITUTION = "es"
    REMOVE_SUBSTITUTION = "ds"
    SELECT_MENTION_TARGET = "mt"
    ADD_MENTION_OVERRIDE = "am"
    EDIT_MENTION_OVERRIDE = "em"
    REMOVE_MENTION_OVERRIDE = "dm"
    SELECT_PRONUNCIATION = "cp"
    ADD_PRONUNCIATION = "ap"
    EDIT_PRONUNCIATION = "ep"
    REMOVE_PRONUNCIATION = "dp"


class VoiceSettingsSection(enum.StrEnum):
    OVERVIEW = "ov"
    VOICE = "vo"
    MENTIONS = "mn"
    SUBSTITUTIONS = "su"
    PRONUNCIATIONS = "pn"


class VoiceAdminActionKind(enum.StrEnum):
    SHOW_SECTION = "ss"
    SHOW_OVERVIEW = "so"
    SHOW_CHANNELS = "sh"
    SHOW_CHANNEL_CONFIG = "cf"
    SHOW_SUBSTITUTION_CATEGORY = "sc"
    SHOW_LINKS_VIEW = "sl"
    SHOW_PRONUNCIATION_CREATE = "sp"
    PAGE = "pg"
    REFRESH = "rf"
    CLOSE = "cl"
    SET_DEFAULT_VOICE = "dv"
    ADD_MODEL = "am"
    SELECT_MODEL_CANDIDATE = "sm"
    REMOVE_MODEL = "rm"
    SELECT_SUBSTITUTION = "cs"
    ADD_SUBSTITUTION = "as"
    EDIT_SUBSTITUTION = "es"
    REMOVE_SUBSTITUTION = "ds"
    SELECT_MENTION_TARGET = "st"
    ADD_MENTION_OVERRIDE = "ao"
    EDIT_MENTION_OVERRIDE = "eo"
    REMOVE_MENTION_OVERRIDE = "do"
    SELECT_PRONUNCIATION = "cp"
    ADD_PRONUNCIATION = "an"
    EDIT_PRONUNCIATION = "en"
    REMOVE_PRONUNCIATION = "dn"
    SET_PRONUNCIATION_VOICE = "pv"
    SET_PRONUNCIATION_FORMAT = "pf"
    OPEN_PRONUNCIATION_MODAL = "pm"
    SELECT_PROTECTED = "ct"
    ADD_PROTECTED = "ap"
    REMOVE_PROTECTED = "dp"
    SELECT_LINK_HOST = "lh"
    ADD_LINK_HOST = "ah"
    EDIT_LINK_HOST = "eh"
    REMOVE_LINK_HOST = "dh"
    SELECT_LINK_RULE = "lr"
    ADD_SIMPLE_LINK_RULE = "ar"
    ADD_COMPLEX_LINK_RULE = "ac"
    EDIT_LINK_RULE = "er"
    REMOVE_LINK_RULE = "dr"
    SELECT_GUILD_VOICE_CHANNEL = "gv"
    SELECT_GUILD_PRIMARY_TTS_CHANNEL = "gt"
    SELECT_GUILD_SECONDARY_TTS_CHANNEL = "gs"
    TOGGLE_PRIMARY_TTS_LISTENING = "tp"
    TOGGLE_SECONDARY_TTS_LISTENING = "ts"
    CLEAR_GUILD_VOICE_CHANNEL = "cv"
    CLEAR_PRIMARY_TTS_CHANNEL = "cpc"
    CLEAR_SECONDARY_TTS_CHANNEL = "csc"
    TOGGLE_RELAY_TTS = "rt"


class VoiceAdminSection(enum.StrEnum):
    OVERVIEW = "ov"
    CHANNELS = "ch"
    MODELS = "mo"
    MENTIONS = "mn"
    SUBSTITUTIONS = "su"
    PRONUNCIATIONS = "pn"
    PROTECTED = "pr"
    LINKS = "li"


class VoiceAdminSubstitutionCategory(enum.StrEnum):
    SLANG = "slang"
    TYPO = "typos"

    @property
    def label(self) -> str:
        return "Slang" if self is VoiceAdminSubstitutionCategory.SLANG else "Typos"


class VoiceAdminLinksView(enum.StrEnum):
    HOSTS = "hosts"
    RULES = "rules"

    @property
    def label(self) -> str:
        return "Hosts" if self is VoiceAdminLinksView.HOSTS else "Rules"


class VoiceAdminPronunciationView(enum.StrEnum):
    LIST = "list"
    CREATE = "create"


class VoiceAdminChannelsView(enum.StrEnum):
    SUMMARY = "summary"
    CONFIG = "config"


@dataclass(slots=True)
class PendingGlobalPronunciation:
    voice: str | None = None
    format: PronunciationFormat = PronunciationFormat.TEXT


@dataclass(frozen=True, slots=True)
class PendingModelScan:
    repo_ref: HFRepoRef
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VoiceSettingsState:
    section: VoiceSettingsSection
    page: int


@dataclass(frozen=True, slots=True)
class VoiceAdminState:
    section: VoiceAdminSection
    page: int
    substitution_category: VoiceAdminSubstitutionCategory = VoiceAdminSubstitutionCategory.SLANG
    links_view: VoiceAdminLinksView = VoiceAdminLinksView.HOSTS
    pronunciation_view: VoiceAdminPronunciationView = VoiceAdminPronunciationView.LIST
    channels_view: VoiceAdminChannelsView = VoiceAdminChannelsView.SUMMARY


@dataclass(slots=True)
class VoiceSettingsSelectionState:
    mention_target_user_id: int | None = None
    substitution_source: str | None = None
    pronunciation_source: str | None = None


@dataclass(slots=True)
class VoiceAdminSelectionState:
    mention_target_user_id: int | None = None
    substitution_source: str | None = None
    pronunciation_value: str | None = None
    protected_token: str | None = None
    link_host: str | None = None
    link_rule_index: int | None = None
    pending_voice_channel_id: int | None = None
    pending_primary_tts_channel_id: int | None = None
    pending_secondary_tts_channel_id: int | None = None


@dataclass(frozen=True, slots=True)
class PagedItems(Generic[ValueT]):
    visible: tuple[ValueT, ...]
    total_count: int
    page_state: EditorPageState


def _editor_flags(is_public: bool) -> hikari.MessageFlag | hikari.UndefinedType:
    if is_public:
        return hikari.UNDEFINED
    return hikari.MessageFlag.EPHEMERAL


def _component_text(value: str, /, *, limit: int = 100) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3].rstrip() + "..."


def _display_value(values: Sequence[str]) -> str:
    return "\n".join(values) if values else "None"


def _substitution_mode_label(case_sensitive: bool) -> str:
    return "case-sensitive" if case_sensitive else "case-insensitive"


def _substitution_rule_label(source: str, rule: TextSubstitutionRule) -> str:
    return f"{source} ({_substitution_mode_label(rule.case_sensitive)})"


def _substitution_rule_value(rule: TextSubstitutionRule) -> str:
    return f"{rule.target} | {_substitution_mode_label(rule.case_sensitive)}"


def _parse_case_sensitive_value(raw: str) -> bool:
    value = raw.strip().lower()
    if not value:
        return False
    truthy = {"true", "t", "yes", "y", "1", "on"}
    falsy = {"false", "f", "no", "n", "0", "off"}
    if value in truthy:
        return True
    if value in falsy:
        return False
    raise ValueError("Case sensitive must be `true` or `false`.")


def _format_case_sensitive_value(case_sensitive: bool) -> str:
    return "true" if case_sensitive else "false"


def _format_voice_link_rule_mode(mode: VoiceLinkRuleMode) -> str:
    return mode.value


def _voice_link_rule_pattern_label(rule: VoiceLinkRule) -> str:
    return f"{'shape' if rule.mode is VoiceLinkRuleMode.SIMPLE else 'regex'}: {rule.input_pattern}"


def _voice_link_rule_value(rule: VoiceLinkRule) -> str:
    example = f" | example: {rule.example_url}" if rule.example_url else ""
    return f"{rule.mode.value} | {_voice_link_rule_pattern_label(rule)} | say: {rule.template}{example}"


def _voice_link_rule_modal_title(mode: VoiceLinkRuleMode) -> str:
    return "Add Simple Link Rule" if mode is VoiceLinkRuleMode.SIMPLE else "Add Complex Link Rule"


def _parse_mention_override_target(raw: str) -> hikari.Snowflake:
    value = raw.strip()
    if not value:
        raise ValueError("Mentioned user must not be empty.")
    if match := USER_MENTION_RE.fullmatch(value):
        return hikari.Snowflake(int(match.group(1)))
    if value.isdigit():
        return hikari.Snowflake(int(value))
    raise ValueError("Mentioned user must be a Discord user mention or user ID.")


def _mention_override_label(
    voice_tts: VoiceTTSService,
    target_user_id: int,
    *,
    guild_id: hikari.Snowflake | None = None,
) -> str:
    if guild_id is not None and (member := voice_tts.bot.cache.get_member(guild_id, target_user_id)):
        return f"{member.display_name} ({target_user_id})"
    if user := voice_tts.bot.cache.get_user(target_user_id):
        display_name = user.display_name or user.username
        return f"{display_name} ({target_user_id})"
    return str(target_user_id)


def _pronunciation_format_label(format: PronunciationFormat) -> str:
    return "IPA" if format is PronunciationFormat.IPA else "Text"


def _pronunciation_override_display(entry: PronunciationOverride) -> str:
    return f"{_pronunciation_format_label(entry.format)}: {entry.value}"


def _page_count(count: int, *, page_size: int = _VOICE_SETTINGS_PAGE_SIZE) -> int:
    return max(1, (count + page_size - 1) // page_size)


def _clamp_page(page: int, total_pages: int) -> int:
    if page < 0:
        return 0
    if page >= total_pages:
        return total_pages - 1
    return page


def _page_slice(values: Sequence[ValueT], page: int, *, page_size: int = _VOICE_SETTINGS_PAGE_SIZE) -> Sequence[ValueT]:
    start = page * page_size
    end = start + page_size
    return values[start:end]


def _paginate(values: Sequence[ValueT], page: int, *, page_size: int = _VOICE_SETTINGS_PAGE_SIZE) -> PagedItems[ValueT]:
    total_pages = _page_count(len(values), page_size=page_size)
    current_page = _clamp_page(page, total_pages)
    return PagedItems(
        visible=tuple(_page_slice(values, current_page, page_size=page_size)),
        total_count=len(values),
        page_state=EditorPageState(page=current_page, total_pages=total_pages),
    )


def _page_for_value(values: Sequence[ValueT], needle: ValueT, *, page_size: int = _VOICE_SETTINGS_PAGE_SIZE) -> int:
    try:
        index = values.index(needle)
    except ValueError:
        return 0
    return index // page_size


def _parse_message_id(raw: str) -> hikari.Snowflake:
    value = raw.strip()
    if not value or not value.isdigit():
        raise ValueError("Editor session is invalid. Reopen the editor and try again.")
    return hikari.Snowflake(int(value))


def _builtin_voice_names(voices: Sequence[str], custom_models: Sequence[str]) -> list[str]:
    custom_voice_names = {model.lower() for model in custom_models}
    return [voice for voice in voices if voice.lower() not in custom_voice_names]


def _voice_connection_status(
    *,
    guild_id: hikari.Snowflake | None,
    voice_tts: VoiceTTSService,
) -> str:
    if guild_id is not None:
        connection = voice_tts.get_connection(guild_id)
        return f"<#{connection.channel_id}>" if connection else "not connected"

    connections = voice_tts.active_voice_connections()
    if not connections:
        return "not connected"
    if len(connections) == 1:
        connection = connections[0]
        return f"<#{connection.channel_id}> in `{connection.guild_id}`"
    return ", ".join(f"`{connection.guild_id}` -> <#{connection.channel_id}>" for connection in connections)


def _channel_reference(channel_id: hikari.Snowflake | None, *, missing: str) -> str:
    if channel_id is None:
        return missing
    return f"<#{int(channel_id)}>"


def _voice_target_saved_status(guild_id: hikari.Snowflake, target: config.VoiceTargetConfig) -> str:
    secondary = (
        f", secondary TTS <#{int(target.secondary_tts_channel)}>"
        if target.secondary_tts_channel is not None
        else ", secondary TTS not set"
    )
    return (
        f"Saved channels for `{guild_id}`: voice <#{int(target.voice_channel)}>, "
        f"primary TTS <#{int(target.primary_tts_channel)}>{secondary}."
    )


def _reset_voice_admin_channel_selection(selection_state: VoiceAdminSelectionState) -> None:
    selection_state.pending_voice_channel_id = None
    selection_state.pending_primary_tts_channel_id = None
    selection_state.pending_secondary_tts_channel_id = None


def _sync_voice_admin_channel_selection(
    selection_state: VoiceAdminSelectionState,
    target: config.VoiceTargetConfig,
) -> None:
    selection_state.pending_voice_channel_id = int(target.voice_channel)
    selection_state.pending_primary_tts_channel_id = int(target.primary_tts_channel)
    selection_state.pending_secondary_tts_channel_id = (
        int(target.secondary_tts_channel) if target.secondary_tts_channel is not None else None
    )


def _voice_settings_state_value(state: VoiceSettingsState) -> str:
    return state.section.value


def _voice_settings_state_from_action(action: object) -> VoiceSettingsState | None:
    page = getattr(action, "page", None)
    raw_section = getattr(action, "value", None)
    if not isinstance(page, int) or not isinstance(raw_section, str):
        return None
    try:
        return VoiceSettingsState(section=VoiceSettingsSection(raw_section), page=page)
    except ValueError:
        return None


def _voice_admin_state_value(state: VoiceAdminState) -> str:
    return _VOICE_ADMIN_STATE_VALUE_SEPARATOR.join(
        (
            state.section.value,
            state.substitution_category.value,
            state.links_view.value,
            state.pronunciation_view.value,
            state.channels_view.value,
        )
    )


def _voice_admin_state_from_action(action: object) -> VoiceAdminState | None:
    page = getattr(action, "page", None)
    raw_value = getattr(action, "value", None)
    if not isinstance(page, int):
        return None
    if raw_value is None:
        return VoiceAdminState(section=VoiceAdminSection.OVERVIEW, page=page)
    if not isinstance(raw_value, str):
        return None

    parts = raw_value.split(_VOICE_ADMIN_STATE_VALUE_SEPARATOR)
    if len(parts) != 5:
        return None

    raw_section, raw_category, raw_links_view, raw_pronunciation_view, raw_channels_view = parts
    try:
        return VoiceAdminState(
            section=VoiceAdminSection(raw_section),
            page=page,
            substitution_category=VoiceAdminSubstitutionCategory(raw_category),
            links_view=VoiceAdminLinksView(raw_links_view),
            pronunciation_view=VoiceAdminPronunciationView(raw_pronunciation_view),
            channels_view=VoiceAdminChannelsView(raw_channels_view),
        )
    except ValueError:
        return None


def _voice_pronunciation_value(voice: str, source: str) -> str:
    return json.dumps([voice, source], separators=(",", ":"))


def _parse_voice_pronunciation_value(raw: str) -> tuple[str, str] | None:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(loaded, list)
        or len(loaded) != 2
        or not isinstance(loaded[0], str)
        or not isinstance(loaded[1], str)
    ):
        return None
    return loaded[0], loaded[1]


async def ac_tts_voices(ctx: lightbulb.AutocompleteContext[str], voice_tts: VoiceTTSService) -> None:
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return
    needle = ctx.focused.value.strip().lower()
    voices = await voice_tts.available_voices()
    if needle:
        voices = [voice for voice in voices if needle in voice.lower()]
    await ctx.respond(voices)


async def ac_tts_variants(ctx: lightbulb.AutocompleteContext[str], voice_tts: VoiceTTSService) -> None:
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    voice_opt = ctx.get_option("voice")
    selected_voice: str | None = None
    if voice_opt and isinstance(voice_opt.value, str):
        selected_voice = voice_opt.value.strip() or None
    if not selected_voice:
        selected_voice, _ = voice_tts.user_voice_variant(ctx.interaction.user.id)

    voices = await voice_tts.available_voices()
    if voices:
        match = next((voice for voice in voices if voice.lower() == selected_voice.lower()), None)
        if match:
            selected_voice = match

    variants = ["none", *await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)]
    await ctx.respond(voice_tts.variant_autocomplete_choices(selected_voice, variants, needle))


async def ac_tts_substitution_sources(ctx: lightbulb.AutocompleteContext[str], voice_tts: VoiceTTSService) -> None:
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    sources = list(voice_tts.user_text_substitutions(ctx.interaction.user.id))
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


async def ac_tts_pronunciation_sources(ctx: lightbulb.AutocompleteContext[str], voice_tts: VoiceTTSService) -> None:
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    current_voice, _ = voice_tts.user_voice_variant(ctx.interaction.user.id)
    sources = list(voice_tts.user_pronunciations(ctx.interaction.user.id, current_voice))
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


async def ac_tts_global_substitution_sources(
    ctx: lightbulb.AutocompleteContext[str],
    voice_tts: VoiceTTSService,
) -> None:
    if not isinstance(ctx.focused.value, str):
        await ctx.respond([])
        return

    category = "slang"
    category_opt = ctx.get_option("category")
    if category_opt and isinstance(category_opt.value, str):
        category = category_opt.value

    try:
        sources = list(voice_tts.global_text_substitutions(category))
    except ValueError:
        await ctx.respond([])
        return

    needle = ctx.focused.value.strip().lower()
    if needle:
        sources = [source for source in sources if needle in source.lower()]
    await ctx.respond(sources[:25])


@group_voice.register
class CMD_VoiceSay(
    lightbulb.SlashCommand,
    name="say",
    description="Queue TTS text from any channel",
):
    text = lightbulb.string("text", "What the bot should say")
    target = lightbulb.string(
        "target",
        "Configured voice target (defaults to the primary guild)",
        default=None,
    )

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        try:
            guild_id = voice_tts.resolve_voice_target_selection(self.target)
        except LookupError as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd say rejected unknown_target user={ctx.user.id} target={self.target!r}")
            return
        if guild_id is None:
            await ctx.respond("Voice TTS is not configured for any server.")
            log.info(f"Voice cmd say rejected no_targets user={ctx.user.id}")
            return

        target_label = await voice_tts.describe_voice_target(guild_id)

        log.info(
            f"Voice cmd say invoked user={ctx.user.id} guild={ctx.guild_id} "
            f"resolved_guild={guild_id} target={target_label!r} text={voice_tts._preview(self.text)!r}"
        )

        try:
            spoken, queue_len = await voice_tts.queue_say(guild_id, ctx.interaction.id, self.text, user_id=ctx.user.id)
        except (RuntimeError, ValueError) as xcp:
            await ctx.respond(str(xcp))
            log.info(f"Voice cmd say rejected user={ctx.user.id} reason={xcp}")
            return

        selected_voice, selected_variant = voice_tts.user_voice_variant_for_say(ctx.user.id)
        voice_spec = voice_tts._voice_spec(selected_voice, selected_variant)
        await ctx.respond(
            "\n".join(
                [
                    f"target: `{target_label}`",
                    f"says `{voice_tts._preview(spoken)}`",
                ]
            )
        )
        log.info(
            f"Voice cmd say success user={ctx.user.id} guild={ctx.guild_id} resolved_guild={guild_id} "
            f"target={target_label!r} queue_size={queue_len} voice={voice_spec} spoken={voice_tts._preview(spoken)!r}"
        )


class VoiceSettingsEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(VoiceSettingsActionKind)
        self._selection_state: dict[hikari.Snowflake, VoiceSettingsSelectionState] = {}
        self._editor = Editor(
            prefix=startup_editor_prefix(_VOICE_SETTINGS_EDITOR_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
        )
        self._substitution_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_SETTINGS_SUBSTITUTION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID,
                        label="Source",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=_VOICE_SUBSTITUTION_SOURCE_FIELD_MAX_LENGTH,
                    ),
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID,
                        label="Target",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_CASE_SENSITIVE_FIELD_ID,
                        label="Case Sensitive",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=8,
                    ),
                ]
            ),
        )
        self._mention_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_SETTINGS_MENTION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID,
                        label="Spoken Name",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                ]
            ),
        )
        self._pronunciation_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_SETTINGS_PRONUNCIATION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID,
                        label="Source",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=64,
                    ),
                    ModalTextField(
                        id=_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID,
                        label="Target",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_SETTINGS_PRONUNCIATION_FORMAT_FIELD_ID,
                        label="Format",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=8,
                    ),
                ]
            ),
        )

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        voice_tts: VoiceTTSService,
        is_public: bool = False,
        status: str = "Manage your voice settings below.",
    ) -> None:
        embed, components = await self._render_editor(
            user_id=hikari.Snowflake(ctx.user.id),
            actor_user_id=hikari.Snowflake(ctx.user.id),
            locale=self._editor.resolve_locale(ctx.interaction),
            guild_id=hikari.Snowflake(ctx.guild_id) if ctx.guild_id is not None else None,
            voice_tts=voice_tts,
            state=VoiceSettingsState(section=VoiceSettingsSection.OVERVIEW, page=0),
        )
        await ctx.respond(status, embed=embed, components=components, flags=_editor_flags(is_public))

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
    ) -> bool:
        return await self._editor.route(interaction, acl=acl, voice_tts=voice_tts)

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
    ) -> bool:
        if await self._substitution_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this voice settings editor.",
            invalid_message="Voice setting input is invalid.",
            acl=acl,
            voice_tts=voice_tts,
        ):
            return True
        if await self._mention_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this voice settings editor.",
            invalid_message="Voice setting input is invalid.",
            acl=acl,
            voice_tts=voice_tts,
        ):
            return True
        return await self._pronunciation_modal.route(
            interaction,
            on_submit=self._on_modal_submit,
            authoriser=self._authorise_modal_submit,
            unauthorised_message="You are not authorised to use this voice settings editor.",
            invalid_message="Voice setting input is invalid.",
            acl=acl,
            voice_tts=voice_tts,
        )

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    async def _authorise_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    async def _authorise_request_user(
        self,
        actor_user_id: hikari.Snowflakeish,
        deps: Mapping[str, object],
    ) -> bool:
        acl = self._require_acl(deps)
        try:
            await acl.perm_check(actor_user_id, acl.LvL.user)
        except Exception:
            return False
        return True

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        acl = self._require_acl(deps)
        voice_tts = self._require_voice_tts(deps)
        await acl.perm_check(req.user_id, acl.LvL.user)

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice settings action.")

        user_id = hikari.Snowflake(req.user_id)
        guild_id = req.interaction.guild_id if req.interaction.guild_id is not None else None
        session_message_id = self._selection_message_id_from_interaction(req.interaction)

        if action.kind is VoiceSettingsActionKind.CLOSE:
            return EditorResponse.close("Voice settings editor closed.")

        state = _voice_settings_state_from_action(action)

        if action.kind in {VoiceSettingsActionKind.PAGE, VoiceSettingsActionKind.REFRESH}:
            if state is None:
                return EditorResponse.ephemeral("Voice settings editor state is invalid.")
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status="Voice settings refreshed."
                if action.kind is VoiceSettingsActionKind.REFRESH
                else "Page updated.",
                force_refresh=action.kind is VoiceSettingsActionKind.REFRESH,
            )

        if action.kind is VoiceSettingsActionKind.SHOW_SECTION:
            if not req.values:
                return EditorResponse.ephemeral("Choose a section first.")
            try:
                section = VoiceSettingsSection(req.values[0])
            except ValueError:
                return EditorResponse.ephemeral("Selected section is invalid.")
            labels = {
                VoiceSettingsSection.OVERVIEW: "overview",
                VoiceSettingsSection.VOICE: "voice settings",
                VoiceSettingsSection.MENTIONS: "mention overrides",
                VoiceSettingsSection.SUBSTITUTIONS: "substitutions",
                VoiceSettingsSection.PRONUNCIATIONS: "pronunciations",
            }
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=section, page=0),
                session_message_id=session_message_id,
                status=f"Showing {labels[section]}.",
            )

        if action.kind is VoiceSettingsActionKind.SHOW_OVERVIEW:
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.OVERVIEW, page=0),
                session_message_id=session_message_id,
                status="Showing overview.",
            )
        if action.kind is VoiceSettingsActionKind.SHOW_VOICE:
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.VOICE, page=0),
                session_message_id=session_message_id,
                status="Showing voice settings.",
            )
        if action.kind is VoiceSettingsActionKind.SHOW_MENTIONS:
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.MENTIONS, page=0),
                session_message_id=session_message_id,
                status="Showing mention overrides.",
            )
        if action.kind is VoiceSettingsActionKind.SHOW_SUBSTITUTIONS:
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.SUBSTITUTIONS, page=0),
                session_message_id=session_message_id,
                status="Showing substitutions.",
            )
        if action.kind is VoiceSettingsActionKind.SHOW_PRONUNCIATIONS:
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.PRONUNCIATIONS, page=0),
                session_message_id=session_message_id,
                status="Showing pronunciations.",
            )

        if action.kind is VoiceSettingsActionKind.TOGGLE_LISTEN:
            enabled = voice_tts.set_user_listening(user_id, not voice_tts.is_user_listening(user_id))
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.VOICE, page=action.page),
                session_message_id=session_message_id,
                status=f"Listening {'enabled' if enabled else 'disabled'}.",
            )

        if action.kind is VoiceSettingsActionKind.TOGGLE_AUTOCORRECT:
            enabled = voice_tts.set_user_autocorrect(user_id, not voice_tts.user_autocorrect_enabled(user_id))
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.VOICE, page=action.page),
                session_message_id=session_message_id,
                status=f"Autocorrect {'enabled' if enabled else 'disabled'}.",
            )

        if action.kind is VoiceSettingsActionKind.SET_VOICE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a voice first.")
            requested_voice = req.values[0]
            try:
                selected_voice, selected_variant = await voice_tts.set_user_voice_variant(
                    user_id, voice=requested_voice
                )
            except (LookupError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            variants = await voice_tts.available_variants_for_voice(selected_voice, force_refresh=True)
            page = (
                _page_for_value(variants, selected_variant, page_size=_VOICE_SETTINGS_VARIANT_PAGE_SIZE)
                if selected_variant is not None
                else 0
            )
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.VOICE, page=page),
                session_message_id=session_message_id,
                status=f"Voice set to `{selected_voice}` with variant `{selected_variant or 'none'}`.",
            )

        if action.kind is VoiceSettingsActionKind.SET_VARIANT:
            if not req.values:
                return EditorResponse.ephemeral("Choose a variant first.")
            requested_variant = "none" if req.values[0] == _VOICE_SETTINGS_CLEAR_VARIANT_VALUE else req.values[0]
            try:
                selected_voice, selected_variant = await voice_tts.set_user_voice_variant(
                    user_id,
                    variant=requested_variant,
                )
            except (LookupError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.VOICE, page=action.page),
                session_message_id=session_message_id,
                status=f"Variant for `{selected_voice}` set to `{selected_variant or 'none'}`.",
            )

        if action.kind in {VoiceSettingsActionKind.ADD_SUBSTITUTION, VoiceSettingsActionKind.EDIT_SUBSTITUTION}:
            values = None
            title = "Add Substitution"
            modal_action = self._action_codec.build(action.kind, page=action.page, value=str(int(session_message_id)))
            if action.kind is VoiceSettingsActionKind.EDIT_SUBSTITUTION:
                source_key = self._selection_state_for_message(session_message_id).substitution_source
                if source_key is None:
                    return EditorResponse.ephemeral("Choose a substitution first.")
                substitutions = voice_tts.user_text_substitutions(user_id)
                rule = substitutions.get(source_key)
                if rule is None:
                    return EditorResponse.ephemeral("That substitution no longer exists.")
                values = {
                    _VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID: source_key,
                    _VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID: rule.target,
                    _VOICE_SETTINGS_ENTRY_CASE_SENSITIVE_FIELD_ID: _format_case_sensitive_value(rule.case_sensitive),
                }
                title = "Edit Substitution"
            await req.interaction.create_modal_response(
                title,
                self._substitution_modal.build_id(modal_action, scope_id=user_id, user_id=user_id),
                components=self._substitution_modal.rows(
                    values
                    or {
                        _VOICE_SETTINGS_ENTRY_CASE_SENSITIVE_FIELD_ID: _format_case_sensitive_value(False),
                    }
                ),
            )
            return None

        if action.kind is VoiceSettingsActionKind.SELECT_SUBSTITUTION:
            if state is None:
                return EditorResponse.ephemeral("Voice settings editor state is invalid.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a substitution first.")
            selected_source = req.values[0]
            substitutions = voice_tts.user_text_substitutions(user_id)
            page = (
                _page_for_value(list(substitutions), selected_source)
                if selected_source in substitutions
                else state.page
            )
            self._selection_state_for_message(session_message_id).substitution_source = selected_source
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.SUBSTITUTIONS, page=page),
                session_message_id=session_message_id,
                status="Selected substitution.",
            )

        if action.kind is VoiceSettingsActionKind.REMOVE_SUBSTITUTION:
            source_key = self._selection_state_for_message(session_message_id).substitution_source
            if source_key is None:
                return EditorResponse.ephemeral("Choose a substitution first.")
            source_key, removed = voice_tts.remove_user_text_substitution(user_id, source_key)
            if removed:
                self._selection_state_for_message(session_message_id).substitution_source = None
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.SUBSTITUTIONS, page=action.page),
                session_message_id=session_message_id,
                status=(
                    f"Removed substitution `{source_key}`." if removed else f"No substitution set for `{source_key}`."
                ),
            )

        if action.kind is VoiceSettingsActionKind.SELECT_MENTION_TARGET:
            if state is None:
                return EditorResponse.ephemeral("Voice settings editor state is invalid.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a user first.")
            if not req.values[0].isdigit():
                return EditorResponse.ephemeral("Selected user is invalid.")
            selected_user_id = int(req.values[0])
            mention_overrides = voice_tts.user_mention_overrides(user_id)
            page = (
                _page_for_value([str(target_uid) for target_uid in mention_overrides], str(selected_user_id))
                if selected_user_id in mention_overrides
                else state.page
            )
            self._selection_state_for_message(session_message_id).mention_target_user_id = selected_user_id
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceSettingsSection.MENTIONS, page=page),
                session_message_id=session_message_id,
                status="Selected mention override target.",
            )

        if action.kind is VoiceSettingsActionKind.ADD_MENTION_OVERRIDE:
            return EditorResponse.ephemeral("Select a user first, then use Edit.")

        if action.kind is VoiceSettingsActionKind.EDIT_MENTION_OVERRIDE:
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            if selected_user_id is None:
                return EditorResponse.ephemeral("Choose a user first.")
            spoken_name = voice_tts.user_mention_overrides(user_id).get(selected_user_id, "")
            await req.interaction.create_modal_response(
                "Edit Mention Override",
                self._mention_modal.build_id(
                    self._action_codec.build(
                        action.kind,
                        page=action.page,
                        value=str(int(session_message_id)),
                    ),
                    scope_id=user_id,
                    user_id=user_id,
                ),
                components=self._mention_modal.rows({_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID: spoken_name}),
            )
            return None

        if action.kind is VoiceSettingsActionKind.REMOVE_MENTION_OVERRIDE:
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            if selected_user_id is None:
                return EditorResponse.ephemeral("Choose a user first.")
            target_user_id, removed = voice_tts.remove_user_mention_override(user_id, selected_user_id)
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.MENTIONS, page=action.page),
                session_message_id=session_message_id,
                status=(
                    f"Removed mention override `{target_user_id}`."
                    if removed
                    else f"No mention override set for `{target_user_id}`."
                ),
            )

        if action.kind in {VoiceSettingsActionKind.ADD_PRONUNCIATION, VoiceSettingsActionKind.EDIT_PRONUNCIATION}:
            current_voice, _ = voice_tts.user_voice_variant(user_id)
            values = None
            title = "Add Pronunciation"
            modal_action = self._action_codec.build(action.kind, page=action.page, value=str(int(session_message_id)))
            if action.kind is VoiceSettingsActionKind.EDIT_PRONUNCIATION:
                source_key = self._selection_state_for_message(session_message_id).pronunciation_source
                if source_key is None:
                    return EditorResponse.ephemeral("Choose a pronunciation first.")
                pronunciations = voice_tts.user_pronunciations(user_id, current_voice)
                entry = pronunciations.get(source_key)
                if entry is None:
                    return EditorResponse.ephemeral("That pronunciation no longer exists.")
                values = {
                    _VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID: source_key,
                    _VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID: entry.value,
                    _VOICE_SETTINGS_PRONUNCIATION_FORMAT_FIELD_ID: entry.format.value,
                }
                title = "Edit Pronunciation"
            await req.interaction.create_modal_response(
                title,
                self._pronunciation_modal.build_id(modal_action, scope_id=user_id, user_id=user_id),
                components=self._pronunciation_modal.rows(values),
            )
            return None

        if action.kind is VoiceSettingsActionKind.SELECT_PRONUNCIATION:
            if state is None:
                return EditorResponse.ephemeral("Voice settings editor state is invalid.")
            if not req.values:
                return EditorResponse.ephemeral("Choose a pronunciation first.")
            selected_source = req.values[0]
            pronunciations = voice_tts.user_pronunciations(user_id, voice_tts.user_voice_variant(user_id)[0])
            page = (
                _page_for_value(list(pronunciations), selected_source)
                if selected_source in pronunciations
                else state.page
            )
            self._selection_state_for_message(session_message_id).pronunciation_source = selected_source
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.PRONUNCIATIONS, page=page),
                session_message_id=session_message_id,
                status="Selected pronunciation.",
            )

        if action.kind is VoiceSettingsActionKind.REMOVE_PRONUNCIATION:
            current_voice, _ = voice_tts.user_voice_variant(user_id)
            source_key = self._selection_state_for_message(session_message_id).pronunciation_source
            if source_key is None:
                return EditorResponse.ephemeral("Choose a pronunciation first.")
            source_key, removed = voice_tts.remove_user_pronunciation(user_id, current_voice, source_key)
            if removed:
                self._selection_state_for_message(session_message_id).pronunciation_source = None
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.PRONUNCIATIONS, page=action.page),
                session_message_id=session_message_id,
                status=(
                    f"Removed pronunciation `{source_key}`."
                    if removed
                    else f"No pronunciation override set for `{source_key}`."
                ),
            )

        return EditorResponse.ephemeral("Unsupported voice settings action.")

    async def _on_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice settings modal action.")

        user_id = hikari.Snowflake(req.user_id)
        guild_id = req.interaction.guild_id if req.interaction.guild_id is not None else None
        target = req.values.get(_VOICE_SETTINGS_ENTRY_TARGET_FIELD_ID, "").strip()
        current_voice, _ = voice_tts.user_voice_variant(user_id)

        if not target:
            return EditorResponse.ephemeral("Target must not be empty.")

        if action.kind in {
            VoiceSettingsActionKind.ADD_MENTION_OVERRIDE,
            VoiceSettingsActionKind.EDIT_MENTION_OVERRIDE,
        }:
            session_message_id = self._selection_message_id_from_action_value(action)
            raw_user = req.values.get(_VOICE_SETTINGS_MENTION_USER_FIELD_ID, "")
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            try:
                if selected_user_id is not None:
                    target_user_id = hikari.Snowflake(selected_user_id)
                else:
                    target_user_id = _parse_mention_override_target(raw_user)
                resolved_user_id, spoken_name, existed = voice_tts.set_user_mention_override(
                    user_id,
                    target_user_id,
                    target,
                )
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            page = _page_for_value(
                [str(target_uid) for target_uid in voice_tts.user_mention_overrides(user_id)],
                str(resolved_user_id),
            )
            self._selection_state_for_message(session_message_id).mention_target_user_id = int(resolved_user_id)
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=self._editor.resolve_locale(req.interaction),
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.MENTIONS, page=page),
                session_message_id=session_message_id,
                status=(
                    f"{'Updated' if existed else 'Added'} mention override `{resolved_user_id}` -> `{spoken_name}`."
                ),
            )

        source = _normalise_voice_source(req.values.get(_VOICE_SETTINGS_ENTRY_SOURCE_FIELD_ID, ""))
        if not source.key:
            return EditorResponse.ephemeral("Source must not be empty.")

        if action.kind in {VoiceSettingsActionKind.ADD_SUBSTITUTION, VoiceSettingsActionKind.EDIT_SUBSTITUTION}:
            session_message_id = self._selection_message_id_from_action_value(action)
            try:
                case_sensitive = _parse_case_sensitive_value(
                    req.values.get(_VOICE_SETTINGS_ENTRY_CASE_SENSITIVE_FIELD_ID, "")
                )
                source_key, replacement, existed = voice_tts.set_user_text_substitution(
                    user_id,
                    source.key,
                    target,
                    case_sensitive=case_sensitive,
                )
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            previous_key = self._selection_state_for_message(session_message_id).substitution_source
            if action.kind is VoiceSettingsActionKind.EDIT_SUBSTITUTION and previous_key and previous_key != source_key:
                voice_tts.remove_user_text_substitution(user_id, previous_key)
            page = _page_for_value(list(voice_tts.user_text_substitutions(user_id)), source_key)
            self._selection_state_for_message(session_message_id).substitution_source = source_key
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=self._editor.resolve_locale(req.interaction),
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.SUBSTITUTIONS, page=page),
                session_message_id=session_message_id,
                status=(
                    f"{'Updated' if existed else 'Added'} substitution "
                    f"`{_substitution_rule_label(source_key, replacement)}` -> `{replacement.target}`."
                ),
            )

        if action.kind in {VoiceSettingsActionKind.ADD_PRONUNCIATION, VoiceSettingsActionKind.EDIT_PRONUNCIATION}:
            session_message_id = self._selection_message_id_from_action_value(action)
            format_raw = req.values.get(_VOICE_SETTINGS_PRONUNCIATION_FORMAT_FIELD_ID, "")
            try:
                pronunciation_format = voice_tts._normalise_pronunciation_format(format_raw)
                source_key, replacement, existed = voice_tts.set_user_pronunciation(
                    user_id,
                    current_voice,
                    source.key,
                    target,
                    pronunciation_format,
                )
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            previous_key = self._selection_state_for_message(session_message_id).pronunciation_source
            if (
                action.kind is VoiceSettingsActionKind.EDIT_PRONUNCIATION
                and previous_key
                and previous_key != source_key
            ):
                voice_tts.remove_user_pronunciation(user_id, current_voice, previous_key)
            page = _page_for_value(list(voice_tts.user_pronunciations(user_id, current_voice)), source_key)
            self._selection_state_for_message(session_message_id).pronunciation_source = source_key
            return await self._build_editor_response(
                user_id=user_id,
                actor_user_id=user_id,
                locale=self._editor.resolve_locale(req.interaction),
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=VoiceSettingsState(section=VoiceSettingsSection.PRONUNCIATIONS, page=page),
                session_message_id=session_message_id,
                status=(
                    f"{'Updated' if existed else 'Added'} pronunciation `{source_key}` "
                    f"-> `{_pronunciation_override_display(replacement)}`."
                ),
            )

        return EditorResponse.ephemeral("Unsupported voice settings modal action.")

    async def _build_editor_response(
        self,
        *,
        user_id: hikari.Snowflake,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        guild_id: hikari.Snowflake | None,
        voice_tts: VoiceTTSService,
        state: VoiceSettingsState,
        session_message_id: hikari.Snowflake | None = None,
        status: str,
        force_refresh: bool = False,
    ) -> EditorResponse:
        embed, components = await self._render_editor(
            user_id=user_id,
            actor_user_id=actor_user_id,
            locale=locale,
            guild_id=guild_id,
            voice_tts=voice_tts,
            state=state,
            session_message_id=session_message_id,
            force_refresh=force_refresh,
        )
        return EditorResponse.update(status, components=components, embeds=[embed])

    async def _render_editor(
        self,
        *,
        user_id: hikari.Snowflake,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        guild_id: hikari.Snowflake | None,
        voice_tts: VoiceTTSService,
        state: VoiceSettingsState,
        session_message_id: hikari.Snowflake | None = None,
        force_refresh: bool = False,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        current_voice, current_variant = voice_tts.user_voice_variant(user_id)
        variants = await voice_tts.available_variants_for_voice(current_voice, force_refresh=force_refresh)
        voices = await voice_tts.available_voices(force_refresh=force_refresh)
        mention_overrides = voice_tts.user_mention_overrides(user_id)
        substitutions = voice_tts.user_text_substitutions(user_id)
        pronunciations = voice_tts.user_pronunciations(user_id, current_voice)
        pronunciation_overrides = voice_tts.user_pronunciation_overrides(user_id, current_voice)
        global_pronunciations = voice_tts.global_pronunciations(current_voice)
        selection_state = self._selection_state.get(session_message_id) if session_message_id is not None else None

        embed = hikari.Embed(
            title="Your Voice Settings",
            description="Manage your personal TTS profile.",
            colour=0x5C4791,
        )

        editor_ctx = self._editor.context(scope_id=user_id, user_id=actor_user_id, locale=locale)
        layout = EditorLayout(editor_ctx)
        self._add_section_selector(layout=layout, state=state)

        if state.section is VoiceSettingsSection.OVERVIEW:
            embed.add_field(
                name="Current Profile",
                value=_display_value(
                    [
                        f"autocorrect: {'enabled' if voice_tts.user_autocorrect_enabled(user_id) else 'disabled'}",
                        f"voice: {current_voice}",
                        f"variant: {current_variant or 'none'}",
                        f"mention overrides: {len(mention_overrides)}",
                        f"substitutions: {len(substitutions)}",
                        f"engine: {voice_tts._engine_display()}",
                        f"connected: {_voice_connection_status(guild_id=guild_id, voice_tts=voice_tts)}",
                    ]
                ),
                inline=False,
            )
            layout.page_footer(
                self._action_codec.build(VoiceSettingsActionKind.CLOSE, page=0),
                page_state=EditorPageState(page=0, total_pages=1),
                extra_buttons=(
                    self._listen_toggle_button(voice_tts=voice_tts, user_id=user_id, page=0),
                    EditorButton(
                        self._build_state_action(VoiceSettingsActionKind.REFRESH, state),
                        "Refresh",
                    ),
                ),
            )
            return embed, layout.build()

        if state.section is VoiceSettingsSection.VOICE:
            variant_page = _paginate(variants, state.page, page_size=_VOICE_SETTINGS_VARIANT_PAGE_SIZE)
            embed.add_field(
                name="Current Voice",
                value=_display_value(
                    [
                        f"voice: {current_voice}",
                        f"variant: {current_variant or 'none'}",
                        f"connected: {_voice_connection_status(guild_id=guild_id, voice_tts=voice_tts)}",
                    ]
                ),
                inline=False,
            )
            layout.add_buttons(
                self._listen_toggle_button(voice_tts=voice_tts, user_id=user_id, page=variant_page.page_state.page),
                self._autocorrect_toggle_button(
                    voice_tts=voice_tts, user_id=user_id, page=variant_page.page_state.page
                ),
            )
            layout.next_row()
            if voices:
                layout.add_text_select(
                    self._action_codec.build(VoiceSettingsActionKind.SET_VOICE, page=variant_page.page_state.page),
                    options=[
                        EditorSelectOption(
                            label=_component_text(voice),
                            value=voice,
                            description="Current voice" if voice == current_voice else "Switch to this voice",
                        )
                        for voice in voices
                    ],
                    placeholder="Choose a voice",
                )
            variant_options = [
                EditorSelectOption(
                    label="None",
                    value=_VOICE_SETTINGS_CLEAR_VARIANT_VALUE,
                    description="Disable the current variant",
                )
            ]
            variant_options.extend(
                EditorSelectOption(
                    label=_component_text(variant),
                    value=variant,
                    description="Current variant" if variant == current_variant else "Switch to this variant",
                )
                for variant in variant_page.visible
            )
            if variant_options:
                layout.add_text_select(
                    self._action_codec.build(VoiceSettingsActionKind.SET_VARIANT, page=variant_page.page_state.page),
                    options=variant_options,
                    placeholder=f"Choose a variant for {current_voice}",
                )
            prev_action = None
            next_action = None
            if variant_page.page_state.total_pages > 1:
                prev_action = self._build_state_action(
                    VoiceSettingsActionKind.PAGE,
                    VoiceSettingsState(
                        section=VoiceSettingsSection.VOICE,
                        page=max(0, variant_page.page_state.page - 1),
                    ),
                )
                next_action = self._build_state_action(
                    VoiceSettingsActionKind.PAGE,
                    VoiceSettingsState(
                        section=VoiceSettingsSection.VOICE,
                        page=min(variant_page.page_state.total_pages - 1, variant_page.page_state.page + 1),
                    ),
                )
            layout.page_footer(
                self._action_codec.build(VoiceSettingsActionKind.CLOSE, page=variant_page.page_state.page),
                page_state=variant_page.page_state,
                prev_action=prev_action,
                next_action=next_action,
                extra_buttons=(
                    EditorButton(self._build_state_action(VoiceSettingsActionKind.REFRESH, state), "Refresh"),
                ),
            )
            return embed, layout.build()

        if state.section is VoiceSettingsSection.MENTIONS:
            await self._render_mention_overrides_section(
                embed=embed,
                layout=layout,
                state=state,
                mention_overrides=mention_overrides,
                guild_id=guild_id,
                session_message_id=session_message_id,
                selected_user_id=selection_state.mention_target_user_id if selection_state is not None else None,
                voice_tts=voice_tts,
            )
            return embed, layout.build()

        if state.section is VoiceSettingsSection.SUBSTITUTIONS:
            await self._render_entry_section(
                embed=embed,
                layout=layout,
                state=state,
                items=substitutions,
                session_message_id=session_message_id,
                selected_key=selection_state.substitution_source if selection_state is not None else None,
                section_title="Substitutions",
                select_action=VoiceSettingsActionKind.SELECT_SUBSTITUTION,
                add_action=VoiceSettingsActionKind.ADD_SUBSTITUTION,
                edit_action=VoiceSettingsActionKind.EDIT_SUBSTITUTION,
                remove_action=VoiceSettingsActionKind.REMOVE_SUBSTITUTION,
                summary_lines=(f"shared base substitutions: {len(voice_tts.base_text_substitutions())}",),
                item_description=lambda value: _substitution_rule_value(value),
                item_label=lambda source, _value: source,
            )
            return embed, layout.build()
        await self._render_pronunciations_section(
            embed=embed,
            layout=layout,
            state=state,
            pronunciations=pronunciations,
            pronunciation_overrides=pronunciation_overrides,
            current_voice=current_voice,
            global_pronunciations=global_pronunciations,
            session_message_id=session_message_id,
            selected_source=selection_state.pronunciation_source if selection_state is not None else None,
            voice_tts=voice_tts,
        )
        return embed, layout.build()

    async def _render_entry_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceSettingsState,
        items: dict[str, ValueT],
        session_message_id: hikari.Snowflake | None,
        selected_key: str | None,
        section_title: str,
        select_action: VoiceSettingsActionKind,
        add_action: VoiceSettingsActionKind,
        edit_action: VoiceSettingsActionKind,
        remove_action: VoiceSettingsActionKind,
        summary_lines: Sequence[str],
        item_description: Callable[[ValueT], str],
        item_label: Callable[[str, ValueT], str] = lambda source, _value: source,
    ) -> None:
        paged = _paginate(list(items.items()), state.page)
        selected_value = items.get(selected_key) if selected_key is not None else None
        selected_label = (
            item_label(selected_key, selected_value)
            if selected_key is not None and selected_value is not None
            else "none"
        )
        summary = [*summary_lines, f"entries: {len(items)}", f"selected: {selected_label}"]
        if selected_value is not None:
            summary.append(f"value: {item_description(selected_value)}")
        embed.add_field(name=section_title, value=_display_value(summary), inline=False)
        if paged.visible:
            layout.add_text_select(
                self._action_codec.build(select_action, page=paged.page_state.page, value=state.section.value),
                options=[
                    EditorSelectOption(
                        label=_component_text(item_label(source, target)),
                        value=source,
                        description=_component_text(item_description(target)),
                        is_default=source == selected_key,
                    )
                    for source, target in paged.visible
                ],
                placeholder=f"Choose {section_title.lower()}",
            )

        prev_action = None
        next_action = None
        if paged.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                VoiceSettingsState(section=state.section, page=max(0, paged.page_state.page - 1)),
            )
            next_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                VoiceSettingsState(
                    section=state.section,
                    page=min(paged.page_state.total_pages - 1, paged.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(VoiceSettingsActionKind.CLOSE, page=paged.page_state.page),
            page_state=paged.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._action_codec.build(add_action, page=paged.page_state.page),
                    "Add",
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                EditorButton(
                    self._action_codec.build(edit_action, page=paged.page_state.page),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_value is None,
                ),
                EditorButton(
                    self._action_codec.build(remove_action, page=paged.page_state.page),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_value is None,
                ),
                EditorButton(self._build_state_action(VoiceSettingsActionKind.REFRESH, state), "Refresh"),
            ),
        )

    async def _render_mention_overrides_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceSettingsState,
        mention_overrides: dict[int, str],
        guild_id: hikari.Snowflake | None,
        session_message_id: hikari.Snowflake | None,
        selected_user_id: int | None,
        voice_tts: VoiceTTSService,
    ) -> None:
        override_items = list(mention_overrides.items())
        paged = _paginate(override_items, state.page)
        selected_override = mention_overrides.get(selected_user_id) if selected_user_id is not None else None

        summary_lines = [
            f"entries: {len(mention_overrides)}",
            "Applied before shared mention overrides.",
            (
                f"selected: {_mention_override_label(voice_tts, selected_user_id, guild_id=guild_id)}"
                if selected_user_id is not None
                else "selected: none"
            ),
            f"override: {selected_override or 'none'}",
        ]
        embed.add_field(name="Mention Overrides", value=_display_value(summary_lines), inline=False)

        visible_lines = [
            f"{_mention_override_label(voice_tts, target_user_id, guild_id=guild_id)}: {spoken_name}"
            for target_user_id, spoken_name in paged.visible
        ]
        embed.add_field(
            name=f"Current Page ({paged.total_count})",
            value=_display_value(visible_lines),
            inline=False,
        )

        layout.add_user_select(
            self._build_state_action(VoiceSettingsActionKind.SELECT_MENTION_TARGET, state),
            placeholder="Choose a user to edit or remove",
        )

        prev_action = None
        next_action = None
        if paged.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                self._state_with(state, page=max(0, paged.page_state.page - 1)),
            )
            next_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                self._state_with(state, page=min(paged.page_state.total_pages - 1, paged.page_state.page + 1)),
            )
        layout.page_footer(
            self._action_codec.build(VoiceSettingsActionKind.CLOSE, page=paged.page_state.page),
            page_state=paged.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(VoiceSettingsActionKind.EDIT_MENTION_OVERRIDE, state),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_user_id is None,
                ),
                EditorButton(
                    self._build_state_action(VoiceSettingsActionKind.REMOVE_MENTION_OVERRIDE, state),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_user_id is None or selected_override is None,
                ),
                EditorButton(self._build_state_action(VoiceSettingsActionKind.REFRESH, state), "Refresh"),
            ),
        )

    async def _render_pronunciations_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceSettingsState,
        pronunciations: dict[str, PronunciationOverride],
        pronunciation_overrides: dict[str, PronunciationOverride],
        current_voice: str,
        global_pronunciations: dict[str, PronunciationOverride],
        session_message_id: hikari.Snowflake | None,
        selected_source: str | None,
        voice_tts: VoiceTTSService,
    ) -> None:
        paged_pronunciations = _paginate(list(pronunciations.items()), state.page)
        selected_entry = pronunciations.get(selected_source) if selected_source is not None else None
        selected_override = pronunciation_overrides.get(selected_source) if selected_source is not None else None
        embed.add_field(
            name="Pronunciations",
            value=_display_value(
                (
                    f"voice: {current_voice}",
                    f"shared base: {len(global_pronunciations)}",
                    f"your overrides: {len(pronunciation_overrides)}",
                    f"effective entries: {len(pronunciations)}",
                    f"ipa: {'available' if voice_tts.voice_supports_ipa_pronunciations(current_voice) else 'unavailable'}",
                    f"selected: {selected_source or 'none'}",
                    (
                        f"value: {_pronunciation_override_display(selected_entry)}"
                        if selected_entry is not None
                        else "value: none"
                    ),
                )
            ),
            inline=False,
        )
        if paged_pronunciations.visible:
            layout.add_text_select(
                self._action_codec.build(
                    VoiceSettingsActionKind.SELECT_PRONUNCIATION,
                    page=paged_pronunciations.page_state.page,
                    value=state.section.value,
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(source),
                        value=source,
                        description=_component_text(_pronunciation_override_display(target)),
                        is_default=source == selected_source,
                    )
                    for source, target in paged_pronunciations.visible
                ],
                placeholder="Choose a pronunciation",
            )

        prev_action = None
        next_action = None
        if paged_pronunciations.page_state.total_pages > 1:
            prev_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                VoiceSettingsState(
                    section=VoiceSettingsSection.PRONUNCIATIONS,
                    page=max(0, paged_pronunciations.page_state.page - 1),
                ),
            )
            next_action = self._build_state_action(
                VoiceSettingsActionKind.PAGE,
                VoiceSettingsState(
                    section=VoiceSettingsSection.PRONUNCIATIONS,
                    page=min(paged_pronunciations.page_state.total_pages - 1, paged_pronunciations.page_state.page + 1),
                ),
            )
        layout.page_footer(
            self._action_codec.build(VoiceSettingsActionKind.CLOSE, page=paged_pronunciations.page_state.page),
            page_state=paged_pronunciations.page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=(
                EditorButton(
                    self._action_codec.build(
                        VoiceSettingsActionKind.ADD_PRONUNCIATION,
                        page=paged_pronunciations.page_state.page,
                        value=str(int(session_message_id)) if session_message_id is not None else None,
                    ),
                    "Add",
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceSettingsActionKind.EDIT_PRONUNCIATION, page=paged_pronunciations.page_state.page
                    ),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_entry is None,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceSettingsActionKind.REMOVE_PRONUNCIATION, page=paged_pronunciations.page_state.page
                    ),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_source is None or selected_override is None,
                ),
                EditorButton(self._build_state_action(VoiceSettingsActionKind.REFRESH, state), "Refresh"),
            ),
        )

    def _listen_toggle_button(
        self,
        *,
        voice_tts: VoiceTTSService,
        user_id: hikari.Snowflake,
        page: int,
    ) -> EditorButton:
        enabled = voice_tts.is_user_listening(user_id)
        return EditorButton(
            self._action_codec.build(VoiceSettingsActionKind.TOGGLE_LISTEN, page=page),
            "Listen On" if enabled else "Listen Off",
            style=hikari.ButtonStyle.SUCCESS if enabled else hikari.ButtonStyle.DANGER,
        )

    def _autocorrect_toggle_button(
        self,
        *,
        voice_tts: VoiceTTSService,
        user_id: hikari.Snowflake,
        page: int,
    ) -> EditorButton:
        enabled = voice_tts.user_autocorrect_enabled(user_id)
        return EditorButton(
            self._action_codec.build(VoiceSettingsActionKind.TOGGLE_AUTOCORRECT, page=page),
            "Autocorrect On" if enabled else "Autocorrect Off",
            style=hikari.ButtonStyle.SUCCESS if enabled else hikari.ButtonStyle.DANGER,
        )

    def _add_section_selector(self, *, layout: EditorLayout, state: VoiceSettingsState) -> None:
        layout.add_text_select(
            self._action_codec.build(VoiceSettingsActionKind.SHOW_SECTION, page=0),
            options=[
                EditorSelectOption(
                    label=label,
                    value=section.value,
                    description=f"Open {label.lower()}",
                    is_default=state.section is section,
                )
                for section, label in (
                    (VoiceSettingsSection.OVERVIEW, "Overview"),
                    (VoiceSettingsSection.VOICE, "Voice"),
                    (VoiceSettingsSection.MENTIONS, "Mentions"),
                    (VoiceSettingsSection.SUBSTITUTIONS, "Substitutions"),
                    (VoiceSettingsSection.PRONUNCIATIONS, "Pronunciations"),
                )
            ],
            placeholder="Choose a settings section",
        )

    def _build_state_action(self, kind: VoiceSettingsActionKind, state: VoiceSettingsState) -> str:
        return self._action_codec.build(kind, page=state.page, value=_voice_settings_state_value(state))

    def _selection_state_for_message(self, message_id: hikari.Snowflakeish) -> VoiceSettingsSelectionState:
        key = hikari.Snowflake(message_id)
        state = self._selection_state.get(key)
        if state is None:
            state = VoiceSettingsSelectionState()
            self._selection_state[key] = state
        return state

    def _selection_message_id_from_interaction(self, interaction: hikari.ComponentInteraction) -> hikari.Snowflake:
        message = interaction.message
        if message is None:
            raise ValueError("Editor session is invalid. Reopen the editor and try again.")
        return hikari.Snowflake(message.id)

    @staticmethod
    def _selection_message_id_from_action_value(action: object) -> hikari.Snowflake:
        raw_value = getattr(action, "value", None)
        if not isinstance(raw_value, str):
            raise ValueError("Editor session is invalid. Reopen the editor and try again.")
        return _parse_message_id(raw_value)

    @staticmethod
    def _state_with(
        state: VoiceSettingsState,
        *,
        section: VoiceSettingsSection | None = None,
        page: int | None = None,
    ) -> VoiceSettingsState:
        return VoiceSettingsState(
            section=state.section if section is None else section,
            page=state.page if page is None else page,
        )

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("Voice settings editor requires Access_Control")
        return value

    @staticmethod
    def _require_voice_tts(deps: Mapping[str, object]) -> VoiceTTSService:
        value = deps.get("voice_tts")
        if not isinstance(value, VoiceTTSService):
            raise TypeError("Voice settings editor requires VoiceTTSService")
        return value


class VoiceAdminEditorService:
    def __init__(self) -> None:
        self._action_codec = PagedActionCodec(VoiceAdminActionKind)
        self._pending_model_scans: dict[hikari.Snowflake, PendingModelScan] = {}
        self._pending_pronunciations: dict[hikari.Snowflake, PendingGlobalPronunciation] = {}
        self._selection_state: dict[hikari.Snowflake, VoiceAdminSelectionState] = {}
        self._editor = Editor(
            prefix=startup_editor_prefix(_VOICE_ADMIN_EDITOR_PREFIX),
            on_action=self._on_editor_action,
            authoriser=self._authorise_editor_action,
            defer_resolver=self._defer_editor_action,
        )
        self._model_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_MODEL_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_MODEL_URL_FIELD_ID,
                        label="Hugging Face URL",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=200,
                    )
                ]
            ),
        )
        self._substitution_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_SUBSTITUTION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_SOURCE_FIELD_ID,
                        label="Source",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=_VOICE_SUBSTITUTION_SOURCE_FIELD_MAX_LENGTH,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_TARGET_FIELD_ID,
                        label="Target",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_CASE_SENSITIVE_FIELD_ID,
                        label="Case Sensitive",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=8,
                    ),
                ]
            ),
        )
        self._entry_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_ENTRY_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_SOURCE_FIELD_ID,
                        label="Source",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=64,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_TARGET_FIELD_ID,
                        label="Target",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                ]
            ),
        )
        self._mention_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_MENTION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_TARGET_FIELD_ID,
                        label="Spoken Name",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                ]
            ),
        )
        self._token_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_TOKEN_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_SOURCE_FIELD_ID,
                        label="Token",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=64,
                    )
                ]
            ),
        )
        self._pronunciation_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_PRONUNCIATION_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_PRONUNCIATION_VOICE_FIELD_ID,
                        label="Voice",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_SOURCE_FIELD_ID,
                        label="Source",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=64,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_TARGET_FIELD_ID,
                        label="Target",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_PRONUNCIATION_FORMAT_FIELD_ID,
                        label="Format",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=8,
                    ),
                ]
            ),
        )
        self._link_host_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_LINK_HOST_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_HOST_FIELD_ID,
                        label="Host",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_LABEL_FIELD_ID,
                        label="Label",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=120,
                    ),
                ]
            ),
        )
        self._link_rule_modal = ModalKit(
            prefix=startup_editor_prefix(_VOICE_ADMIN_LINK_RULE_MODAL_PREFIX),
            schema=ModalSchema(
                [
                    ModalTextField(
                        id=_VOICE_ADMIN_LINK_RULE_URL_FIELD_ID,
                        label="Example URL",
                        style=hikari.TextInputStyle.SHORT,
                        required=True,
                        max_length=200,
                        placeholder="https://store.steampowered.com/app/3493540/Transport_Fever_2/",
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_PATH_REGEX_FIELD_ID,
                        label="Path Pattern",
                        style=hikari.TextInputStyle.SHORT,
                        required=False,
                        max_length=200,
                        placeholder="simple: /app/{id}/{title} | regex: ^/app/\\d+/(?P<title>[^/?#]+)",
                    ),
                    ModalTextField(
                        id=_VOICE_ADMIN_TEMPLATE_FIELD_ID,
                        label="Speak As",
                        style=hikari.TextInputStyle.PARAGRAPH,
                        required=True,
                        max_length=200,
                        placeholder="steam store {title_norm}",
                    ),
                ]
            ),
        )

    async def open_editor(
        self,
        *,
        ctx: lightbulb.Context,
        voice_tts: VoiceTTSService,
        is_public: bool = False,
        status: str = "Manage shared voice settings below.",
    ) -> None:
        embed, components = await self._render_editor(
            actor_user_id=hikari.Snowflake(ctx.user.id),
            locale=self._editor.resolve_locale(ctx.interaction),
            guild_id=hikari.Snowflake(ctx.guild_id) if ctx.guild_id is not None else None,
            voice_tts=voice_tts,
            state=VoiceAdminState(section=VoiceAdminSection.OVERVIEW, page=0),
        )
        await ctx.respond(status, embed=embed, components=components, flags=_editor_flags(is_public))

    async def route_component(
        self,
        interaction: hikari.ComponentInteraction,
        *,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
    ) -> bool:
        return await self._editor.route(interaction, acl=acl, voice_tts=voice_tts)

    async def route_modal(
        self,
        interaction: hikari.ModalInteraction,
        *,
        acl: Access_Control,
        voice_tts: VoiceTTSService,
    ) -> bool:
        for kit, handler, defer_resolver in (
            (self._model_modal, self._on_model_modal_submit, self._defer_model_modal_submit),
            (self._substitution_modal, self._on_substitution_modal_submit, None),
            (self._mention_modal, self._on_mention_modal_submit, None),
            (self._entry_modal, self._on_entry_modal_submit, None),
            (self._token_modal, self._on_token_modal_submit, None),
            (self._pronunciation_modal, self._on_pronunciation_modal_submit, None),
            (self._link_host_modal, self._on_link_host_modal_submit, None),
            (self._link_rule_modal, self._on_link_rule_modal_submit, None),
        ):
            handled = await kit.route(
                interaction,
                on_submit=handler,
                authoriser=self._authorise_modal_submit,
                defer_resolver=defer_resolver,
                unauthorised_message="You are not authorised to use this voice admin editor.",
                invalid_message="Submitted values were invalid.",
                acl=acl,
                voice_tts=voice_tts,
            )
            if handled:
                return True
        return False

    async def _authorise_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    def _defer_editor_action(
        self,
        req: EditorRequest,
        deps: Mapping[str, object],
    ) -> InteractionDeferral | None:
        action = self._action_codec.parse(req.action)
        if action is None:
            return None
        if action.kind is not VoiceAdminActionKind.SELECT_MODEL_CANDIDATE:
            return None
        return InteractionDeferral.update()

    async def _authorise_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> bool:
        return await self._authorise_request_user(req.user_id, deps)

    def _defer_model_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> InteractionDeferral | None:
        action = self._action_codec.parse(req.action)
        if action is None:
            return None
        if action.kind is not VoiceAdminActionKind.ADD_MODEL:
            return None
        return InteractionDeferral.update()

    async def _authorise_request_user(
        self,
        actor_user_id: hikari.Snowflakeish,
        deps: Mapping[str, object],
    ) -> bool:
        acl = self._require_acl(deps)
        try:
            await acl.perm_check(actor_user_id, acl.LvL.admin)
        except Exception:
            return False
        return True

    async def _on_editor_action(self, req: EditorRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        acl = self._require_acl(deps)
        voice_tts = self._require_voice_tts(deps)
        await acl.perm_check(req.user_id, acl.LvL.admin)

        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin action.")

        actor_user_id = hikari.Snowflake(req.user_id)
        guild_id = req.interaction.guild_id if req.interaction.guild_id is not None else None
        state = _voice_admin_state_from_action(action)
        session_message_id = self._selection_message_id_from_interaction(req.interaction)

        if action.kind is VoiceAdminActionKind.CLOSE:
            return EditorResponse.close("Voice admin editor closed.")

        if action.kind in {
            VoiceAdminActionKind.SHOW_OVERVIEW,
            VoiceAdminActionKind.SHOW_CHANNELS,
            VoiceAdminActionKind.SHOW_CHANNEL_CONFIG,
            VoiceAdminActionKind.SHOW_SECTION,
            VoiceAdminActionKind.SHOW_SUBSTITUTION_CATEGORY,
            VoiceAdminActionKind.SHOW_LINKS_VIEW,
            VoiceAdminActionKind.SHOW_PRONUNCIATION_CREATE,
            VoiceAdminActionKind.PAGE,
            VoiceAdminActionKind.REFRESH,
        }:
            if state is None:
                return EditorResponse.ephemeral("Voice admin state is invalid.")
            if action.kind is VoiceAdminActionKind.SHOW_OVERVIEW:
                state = self._state_with(state, section=VoiceAdminSection.OVERVIEW, page=0)
            elif action.kind is VoiceAdminActionKind.SHOW_CHANNELS:
                state = self._state_with(
                    state,
                    section=VoiceAdminSection.CHANNELS,
                    channels_view=VoiceAdminChannelsView.SUMMARY,
                    page=0,
                )
            elif action.kind is VoiceAdminActionKind.SHOW_CHANNEL_CONFIG:
                state = self._state_with(
                    state,
                    section=VoiceAdminSection.CHANNELS,
                    channels_view=VoiceAdminChannelsView.CONFIG,
                    page=0,
                )
            elif action.kind is VoiceAdminActionKind.SHOW_SECTION and req.values:
                try:
                    selected_section = VoiceAdminSection(req.values[0])
                except ValueError:
                    return EditorResponse.ephemeral("Selected section is invalid.")
                state = self._state_with(
                    state,
                    section=selected_section,
                    page=0,
                    channels_view=(
                        VoiceAdminChannelsView.SUMMARY
                        if selected_section is VoiceAdminSection.CHANNELS
                        else state.channels_view
                    ),
                    pronunciation_view=(
                        VoiceAdminPronunciationView.LIST
                        if selected_section is VoiceAdminSection.PRONUNCIATIONS
                        else state.pronunciation_view
                    ),
                )
            elif action.kind is VoiceAdminActionKind.SHOW_SUBSTITUTION_CATEGORY and req.values:
                try:
                    state = self._state_with(
                        state,
                        substitution_category=VoiceAdminSubstitutionCategory(req.values[0]),
                        page=0,
                    )
                except ValueError:
                    return EditorResponse.ephemeral("Selected substitution category is invalid.")
            elif action.kind is VoiceAdminActionKind.SHOW_LINKS_VIEW and req.values:
                try:
                    state = self._state_with(state, links_view=VoiceAdminLinksView(req.values[0]), page=0)
                except ValueError:
                    return EditorResponse.ephemeral("Selected links view is invalid.")
            elif action.kind is VoiceAdminActionKind.SHOW_PRONUNCIATION_CREATE:
                self._pending_pronunciations[actor_user_id] = PendingGlobalPronunciation(
                    voice=voice_tts.voice,
                    format=PronunciationFormat.TEXT,
                )
                state = self._state_with(
                    state,
                    section=VoiceAdminSection.PRONUNCIATIONS,
                    page=0,
                    pronunciation_view=VoiceAdminPronunciationView.CREATE,
                )
            status = (
                "Showing section."
                if action.kind
                in {
                    VoiceAdminActionKind.SHOW_OVERVIEW,
                    VoiceAdminActionKind.SHOW_CHANNELS,
                    VoiceAdminActionKind.SHOW_CHANNEL_CONFIG,
                    VoiceAdminActionKind.SHOW_SECTION,
                    VoiceAdminActionKind.SHOW_SUBSTITUTION_CATEGORY,
                    VoiceAdminActionKind.SHOW_LINKS_VIEW,
                    VoiceAdminActionKind.SHOW_PRONUNCIATION_CREATE,
                }
                else "Page updated."
                if action.kind is VoiceAdminActionKind.PAGE
                else "Voice admin refreshed."
            )
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=status,
                force_refresh=action.kind is VoiceAdminActionKind.REFRESH,
            )

        if action.kind is VoiceAdminActionKind.ADD_MODEL:
            model_url = ""
            await req.interaction.create_modal_response(
                "Add Voice Model",
                self._model_modal.build_id(req.action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._model_modal.rows({_VOICE_ADMIN_MODEL_URL_FIELD_ID: model_url}),
            )
            return None

        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")

        if action.kind is VoiceAdminActionKind.SELECT_MODEL_CANDIDATE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a model file first.")
            pending_scan = self._pending_model_scans.get(actor_user_id)
            if pending_scan is None:
                return EditorResponse.ephemeral("No pending model scan found. Start with Add Voice.")
            selected_file = req.values[0]
            if selected_file not in pending_scan.candidates:
                return EditorResponse.ephemeral("Selected model file is invalid.")
            await req.interaction.edit_initial_response(
                content=(
                    f"Downloading `{Path(selected_file).name}` from "
                    f"`{pending_scan.repo_ref.repo_id}` ({pending_scan.repo_ref.revision})..."
                ),
                components=[],
            )
            try:
                model_name, has_config = await voice_tts.add_piper_model_from_hf(pending_scan.repo_ref, selected_file)
            except FileExistsError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            except (LookupError, RuntimeError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            except requests.RequestException as xcp:
                return EditorResponse.ephemeral(f"Failed to download model: {xcp}")

            self._pending_model_scans.pop(actor_user_id, None)
            custom_models = voice_tts.available_custom_voices()
            page = _page_for_value(custom_models, model_name) if model_name in custom_models else 0
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.MODELS, page=page),
                session_message_id=session_message_id,
                status=(
                    f"Added TTS model `{model_name}` "
                    f"(config: `{'downloaded' if has_config else 'not found'}`). "
                    f"Use `{_VOICE_SETTINGS_COMMAND}` to switch to it."
                ),
                force_refresh=True,
            )

        if action.kind is VoiceAdminActionKind.SET_DEFAULT_VOICE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a built-in voice first.")
            try:
                selected_voice, selected_variant = await voice_tts.set_voice_variant(voice=req.values[0])
            except (LookupError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.MODELS),
                session_message_id=session_message_id,
                status=f"Default voice set to `{selected_voice}` with variant `{selected_variant or 'none'}`.",
                force_refresh=True,
            )

        if action.kind is VoiceAdminActionKind.SET_PRONUNCIATION_VOICE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a voice first.")
            pending = self._pending_pronunciations.get(actor_user_id)
            if pending is None:
                pending = PendingGlobalPronunciation(format=PronunciationFormat.TEXT)
                self._pending_pronunciations[actor_user_id] = pending
            requested_voice = req.values[0]
            try:
                pending.voice = await voice_tts._resolve_requested_voice(requested_voice)
            except (LookupError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.PRONUNCIATIONS,
                    pronunciation_view=VoiceAdminPronunciationView.CREATE,
                ),
                session_message_id=session_message_id,
                status=f"Pronunciation voice set to `{pending.voice}`.",
            )

        if action.kind is VoiceAdminActionKind.SET_PRONUNCIATION_FORMAT:
            if not req.values:
                return EditorResponse.ephemeral("Choose a format first.")
            pending = self._pending_pronunciations.get(actor_user_id)
            if pending is None:
                pending = PendingGlobalPronunciation(voice=voice_tts.voice)
                self._pending_pronunciations[actor_user_id] = pending
            try:
                pending.format = voice_tts._normalise_pronunciation_format(req.values[0])
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.PRONUNCIATIONS,
                    pronunciation_view=VoiceAdminPronunciationView.CREATE,
                ),
                session_message_id=session_message_id,
                status=f"Pronunciation format set to `{pending.format.value}`.",
            )

        if action.kind is VoiceAdminActionKind.OPEN_PRONUNCIATION_MODAL:
            pending = self._pending_pronunciations.get(actor_user_id)
            if pending is None or pending.voice is None:
                return EditorResponse.ephemeral("Choose a voice first.")
            await req.interaction.create_modal_response(
                "Add Global Pronunciation",
                self._entry_modal.build_id(req.action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._entry_modal.rows(),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_MODEL:
            if not req.values:
                return EditorResponse.ephemeral("Choose a custom voice model first.")
            try:
                removed = await voice_tts.delete_piper_model(req.values[0])
            except LookupError:
                return EditorResponse.ephemeral(f"Unknown model `{req.values[0]}`.")
            except (RuntimeError, ValueError) as xcp:
                return EditorResponse.ephemeral(str(xcp))
            custom_models = voice_tts.available_custom_voices()
            page = _page_for_value(custom_models, removed) if removed in custom_models else 0
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, page=page),
                session_message_id=session_message_id,
                status=f"Deleted TTS model `{removed}`.",
                force_refresh=True,
            )

        if action.kind in {
            VoiceAdminActionKind.TOGGLE_PRIMARY_TTS_LISTENING,
            VoiceAdminActionKind.TOGGLE_SECONDARY_TTS_LISTENING,
        }:
            if guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage channels.")
            target = voice_tts.voice_target(guild_id)
            if target is None:
                return EditorResponse.ephemeral("Configure voice and TTS channels first.")
            role = (
                config.VoiceTargetTtsChannelRole.PRIMARY
                if action.kind is VoiceAdminActionKind.TOGGLE_PRIMARY_TTS_LISTENING
                else config.VoiceTargetTtsChannelRole.SECONDARY
            )
            if role is config.VoiceTargetTtsChannelRole.SECONDARY and target.secondary_tts_channel is None:
                return EditorResponse.ephemeral("Configure a secondary TTS channel first.")
            updated_target = voice_tts.set_voice_target_tts_listen_enabled(
                guild_id,
                role,
                not target.tts_channel_listen_enabled(role),
            )
            if session_message_id is not None:
                _sync_voice_admin_channel_selection(
                    self._selection_state_for_message(session_message_id),
                    updated_target,
                )
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.CHANNELS, page=0),
                session_message_id=session_message_id,
                status=f"{role.label.capitalize()} TTS listening {'enabled' if updated_target.tts_channel_listen_enabled(role) else 'disabled'}.",
            )

        if action.kind in {
            VoiceAdminActionKind.SELECT_GUILD_VOICE_CHANNEL,
            VoiceAdminActionKind.SELECT_GUILD_PRIMARY_TTS_CHANNEL,
            VoiceAdminActionKind.SELECT_GUILD_SECONDARY_TTS_CHANNEL,
            VoiceAdminActionKind.CLEAR_GUILD_VOICE_CHANNEL,
            VoiceAdminActionKind.CLEAR_PRIMARY_TTS_CHANNEL,
            VoiceAdminActionKind.CLEAR_SECONDARY_TTS_CHANNEL,
        }:
            if guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage channels.")
            if session_message_id is None:
                return EditorResponse.ephemeral("Voice admin message is unavailable.")
            if action.kind in {
                VoiceAdminActionKind.SELECT_GUILD_VOICE_CHANNEL,
                VoiceAdminActionKind.SELECT_GUILD_PRIMARY_TTS_CHANNEL,
                VoiceAdminActionKind.SELECT_GUILD_SECONDARY_TTS_CHANNEL,
            } and not req.values:
                prompt = (
                    "Choose a voice channel first."
                    if action.kind is VoiceAdminActionKind.SELECT_GUILD_VOICE_CHANNEL
                    else "Choose a TTS channel first."
                )
                return EditorResponse.ephemeral(prompt)

            selection_state = self._selection_state_for_message(session_message_id)
            current_target = voice_tts.voice_target(guild_id)

            if action.kind is VoiceAdminActionKind.CLEAR_GUILD_VOICE_CHANNEL:
                voice_tts.remove_voice_target_config(guild_id)
                _reset_voice_admin_channel_selection(selection_state)
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status="Cleared voice channel. Voice TTS config removed for this guild.",
                )

            if action.kind is VoiceAdminActionKind.CLEAR_PRIMARY_TTS_CHANNEL:
                voice_tts.remove_voice_target_config(guild_id)
                _reset_voice_admin_channel_selection(selection_state)
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status="Cleared primary TTS channel. Voice TTS config removed for this guild.",
                )

            if action.kind is VoiceAdminActionKind.CLEAR_SECONDARY_TTS_CHANNEL:
                selection_state.pending_secondary_tts_channel_id = None
                if current_target is not None:
                    updated_target = voice_tts.set_voice_target_config(
                        guild_id,
                        voice_channel=current_target.voice_channel,
                        primary_tts_channel=current_target.primary_tts_channel,
                        secondary_tts_channel=None,
                    )
                    _sync_voice_admin_channel_selection(selection_state, updated_target)
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status="Secondary TTS channel cleared.",
                )

            selected_channel_id = hikari.Snowflake(req.values[0])

            if action.kind is VoiceAdminActionKind.SELECT_GUILD_VOICE_CHANNEL:
                selection_state.pending_voice_channel_id = int(selected_channel_id)
                primary_tts_channel_id = (
                    hikari.Snowflake(selection_state.pending_primary_tts_channel_id)
                    if selection_state.pending_primary_tts_channel_id is not None
                    else current_target.primary_tts_channel if current_target is not None else None
                )
                secondary_tts_channel_id = (
                    hikari.Snowflake(selection_state.pending_secondary_tts_channel_id)
                    if selection_state.pending_secondary_tts_channel_id is not None
                    else current_target.secondary_tts_channel if current_target is not None else None
                )
                if primary_tts_channel_id is None:
                    return await self._build_editor_response(
                        actor_user_id=actor_user_id,
                        locale=req.locale,
                        guild_id=guild_id,
                        voice_tts=voice_tts,
                        state=self._state_with(
                            state,
                            section=VoiceAdminSection.CHANNELS,
                            channels_view=VoiceAdminChannelsView.CONFIG,
                            page=0,
                        ),
                        session_message_id=session_message_id,
                        status="Voice channel selected. Choose a primary TTS channel to finish setup.",
                    )

                try:
                    target = voice_tts.set_voice_target_config(
                        guild_id,
                        voice_channel=selected_channel_id,
                        primary_tts_channel=primary_tts_channel_id,
                        secondary_tts_channel=secondary_tts_channel_id,
                    )
                except ValueError as xcp:
                    return EditorResponse.ephemeral(str(xcp))
                _sync_voice_admin_channel_selection(selection_state, target)
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status=_voice_target_saved_status(guild_id, target),
                )

            if action.kind is VoiceAdminActionKind.SELECT_GUILD_PRIMARY_TTS_CHANNEL:
                selection_state.pending_primary_tts_channel_id = int(selected_channel_id)
            else:
                selection_state.pending_secondary_tts_channel_id = int(selected_channel_id)
            voice_channel_id = (
                hikari.Snowflake(selection_state.pending_voice_channel_id)
                if selection_state.pending_voice_channel_id is not None
                else current_target.voice_channel if current_target is not None else None
            )
            primary_tts_channel_id = (
                hikari.Snowflake(selection_state.pending_primary_tts_channel_id)
                if selection_state.pending_primary_tts_channel_id is not None
                else current_target.primary_tts_channel if current_target is not None else None
            )
            secondary_tts_channel_id = (
                hikari.Snowflake(selection_state.pending_secondary_tts_channel_id)
                if selection_state.pending_secondary_tts_channel_id is not None
                else current_target.secondary_tts_channel if current_target is not None else None
            )
            role_label = (
                "primary"
                if action.kind is VoiceAdminActionKind.SELECT_GUILD_PRIMARY_TTS_CHANNEL
                else "secondary"
            )
            if voice_channel_id is None:
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status=f"{role_label.capitalize()} TTS channel selected. Choose a voice channel to finish setup.",
                )

            if primary_tts_channel_id is None:
                return await self._build_editor_response(
                    actor_user_id=actor_user_id,
                    locale=req.locale,
                    guild_id=guild_id,
                    voice_tts=voice_tts,
                    state=self._state_with(
                        state,
                        section=VoiceAdminSection.CHANNELS,
                        channels_view=VoiceAdminChannelsView.CONFIG,
                        page=0,
                    ),
                    session_message_id=session_message_id,
                    status="Choose a primary TTS channel to finish setup.",
                )

            try:
                target = voice_tts.set_voice_target_config(
                    guild_id,
                    voice_channel=voice_channel_id,
                    primary_tts_channel=primary_tts_channel_id,
                    secondary_tts_channel=secondary_tts_channel_id,
                )
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            _sync_voice_admin_channel_selection(selection_state, target)
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.CHANNELS,
                    channels_view=VoiceAdminChannelsView.CONFIG,
                    page=0,
                ),
                session_message_id=session_message_id,
                status=_voice_target_saved_status(guild_id, target),
            )

        if action.kind is VoiceAdminActionKind.TOGGLE_RELAY_TTS:
            if guild_id is None:
                return EditorResponse.ephemeral("Open this editor in a server to manage channels.")
            target = voice_tts.voice_target(guild_id)
            if target is None:
                return EditorResponse.ephemeral("Configure voice and TTS channels first.")
            updated_target = voice_tts.set_voice_target_relay_tts_enabled(guild_id, not target.relay_tts_enabled)
            if session_message_id is not None:
                selection_state = self._selection_state_for_message(session_message_id)
                _sync_voice_admin_channel_selection(selection_state, updated_target)
            enabled = updated_target.relay_tts_enabled
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.CHANNELS, page=0),
                session_message_id=session_message_id,
                status=(
                    "Relay TTS enabled. Linked users with Listen On will use their own voices when game relay "
                    "messages land in either configured TTS channel."
                    if enabled
                    else "Relay TTS disabled for this guild."
                ),
            )

        if action.kind is VoiceAdminActionKind.SELECT_MENTION_TARGET:
            if not req.values:
                return EditorResponse.ephemeral("Choose a user first.")
            if not req.values[0].isdigit():
                return EditorResponse.ephemeral("Selected user is invalid.")
            selected_user_id = int(req.values[0])
            mention_overrides = voice_tts.global_mention_overrides()
            page = (
                _page_for_value([str(target_uid) for target_uid in mention_overrides], str(selected_user_id))
                if selected_user_id in mention_overrides
                else state.page
            )
            self._selection_state_for_message(session_message_id).mention_target_user_id = selected_user_id
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.MENTIONS, page=page),
                session_message_id=session_message_id,
                status="Selected shared mention override target.",
            )

        if action.kind is VoiceAdminActionKind.SELECT_SUBSTITUTION:
            if not req.values:
                return EditorResponse.ephemeral("Choose a substitution first.")
            selected_source = req.values[0]
            substitutions = voice_tts.global_text_substitutions(state.substitution_category.value)
            page = (
                _page_for_value(list(substitutions), selected_source)
                if selected_source in substitutions
                else state.page
            )
            self._selection_state_for_message(session_message_id).substitution_source = selected_source
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.SUBSTITUTIONS, page=page),
                session_message_id=session_message_id,
                status="Selected shared substitution.",
            )

        if action.kind in {VoiceAdminActionKind.ADD_SUBSTITUTION, VoiceAdminActionKind.EDIT_SUBSTITUTION}:
            title = "Add Shared Substitution"
            values = None
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            if action.kind is VoiceAdminActionKind.EDIT_SUBSTITUTION:
                source_key = self._selection_state_for_message(session_message_id).substitution_source
                if source_key is None:
                    return EditorResponse.ephemeral("Choose a substitution first.")
                substitutions = voice_tts.global_text_substitutions(state.substitution_category.value)
                rule = substitutions.get(source_key)
                if rule is None:
                    return EditorResponse.ephemeral("That substitution no longer exists.")
                title = "Edit Shared Substitution"
                values = {
                    _VOICE_ADMIN_SOURCE_FIELD_ID: source_key,
                    _VOICE_ADMIN_TARGET_FIELD_ID: rule.target,
                    _VOICE_ADMIN_CASE_SENSITIVE_FIELD_ID: _format_case_sensitive_value(rule.case_sensitive),
                }
            await req.interaction.create_modal_response(
                title,
                self._substitution_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._substitution_modal.rows(
                    values
                    or {
                        _VOICE_ADMIN_CASE_SENSITIVE_FIELD_ID: _format_case_sensitive_value(False),
                    }
                ),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_SUBSTITUTION:
            selected_source = self._selection_state_for_message(session_message_id).substitution_source
            if selected_source is None:
                return EditorResponse.ephemeral("Choose a substitution first.")
            try:
                category_key, source_key, removed = voice_tts.remove_global_text_substitution(
                    state.substitution_category.value,
                    selected_source,
                )
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            if removed:
                self._selection_state_for_message(session_message_id).substitution_source = None
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=(
                    f"Removed global {category_key}: `{source_key}`."
                    if removed
                    else f"No global {category_key} set for `{source_key}`."
                ),
            )

        if action.kind is VoiceAdminActionKind.ADD_MENTION_OVERRIDE:
            return EditorResponse.ephemeral("Select a user first, then use Edit.")

        if action.kind is VoiceAdminActionKind.EDIT_MENTION_OVERRIDE:
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            if selected_user_id is None:
                return EditorResponse.ephemeral("Choose a user first.")
            spoken_name = voice_tts.global_mention_overrides().get(selected_user_id, "")
            await req.interaction.create_modal_response(
                "Edit Shared Mention Override",
                self._mention_modal.build_id(
                    self._action_codec.build(
                        action.kind,
                        page=state.page,
                        value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
                    ),
                    scope_id=actor_user_id,
                    user_id=actor_user_id,
                ),
                components=self._mention_modal.rows({_VOICE_ADMIN_TARGET_FIELD_ID: spoken_name}),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_MENTION_OVERRIDE:
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            if selected_user_id is None:
                return EditorResponse.ephemeral("Choose a user first.")
            target_user_id, removed = voice_tts.remove_global_mention_override(selected_user_id)
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.MENTIONS),
                session_message_id=session_message_id,
                status=(
                    f"Removed shared mention override `{target_user_id}`."
                    if removed
                    else f"No shared mention override set for `{target_user_id}`."
                ),
            )

        if action.kind is VoiceAdminActionKind.ADD_PRONUNCIATION:
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.PRONUNCIATIONS,
                    pronunciation_view=VoiceAdminPronunciationView.CREATE,
                ),
                session_message_id=session_message_id,
                status="Showing pronunciation creator.",
            )

        if action.kind is VoiceAdminActionKind.SELECT_PRONUNCIATION:
            if not req.values:
                return EditorResponse.ephemeral("Choose a pronunciation first.")
            selected_value = req.values[0]
            flattened_pronunciations = [
                (entry_voice, entry_source, entry)
                for entry_voice, entries in sorted(voice_tts.all_global_pronunciations().items())
                for entry_source, entry in entries.items()
            ]
            page = _page_for_value(
                [
                    _voice_pronunciation_value(entry_voice, entry_source)
                    for entry_voice, entry_source, _ in flattened_pronunciations
                ],
                selected_value,
            )
            self._selection_state_for_message(session_message_id).pronunciation_value = selected_value
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.PRONUNCIATIONS, page=page),
                session_message_id=session_message_id,
                status="Selected global pronunciation.",
            )

        if action.kind is VoiceAdminActionKind.EDIT_PRONUNCIATION:
            title = "Add Global Pronunciation"
            values = None
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            pronunciation_value = self._selection_state_for_message(session_message_id).pronunciation_value
            if pronunciation_value is None:
                return EditorResponse.ephemeral("Choose a pronunciation first.")
            parsed = _parse_voice_pronunciation_value(pronunciation_value)
            if parsed is None:
                return EditorResponse.ephemeral("Pronunciation selection is invalid.")
            voice_key, source_key = parsed
            entry = voice_tts.global_pronunciations(voice_key).get(source_key)
            if entry is None:
                return EditorResponse.ephemeral("That global pronunciation no longer exists.")
            title = "Edit Global Pronunciation"
            values = {
                _VOICE_ADMIN_PRONUNCIATION_VOICE_FIELD_ID: voice_key,
                _VOICE_ADMIN_SOURCE_FIELD_ID: source_key,
                _VOICE_ADMIN_TARGET_FIELD_ID: entry.value,
                _VOICE_ADMIN_PRONUNCIATION_FORMAT_FIELD_ID: entry.format.value,
            }
            await req.interaction.create_modal_response(
                title,
                self._pronunciation_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._pronunciation_modal.rows(values),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_PRONUNCIATION:
            pronunciation_value = self._selection_state_for_message(session_message_id).pronunciation_value
            if pronunciation_value is None:
                return EditorResponse.ephemeral("Choose a pronunciation first.")
            parsed = _parse_voice_pronunciation_value(pronunciation_value)
            if parsed is None:
                return EditorResponse.ephemeral("Pronunciation selection is invalid.")
            voice_key, source_key = parsed
            try:
                removed_voice, removed_source, removed = voice_tts.remove_global_pronunciation(voice_key, source_key)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            if removed:
                self._selection_state_for_message(session_message_id).pronunciation_value = None
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=(
                    f"Removed global pronunciation `{removed_voice}` / `{removed_source}`."
                    if removed
                    else f"No global pronunciation set for `{removed_voice}` / `{removed_source}`."
                ),
            )

        if action.kind is VoiceAdminActionKind.SELECT_PROTECTED:
            if not req.values:
                return EditorResponse.ephemeral("Choose a protected token first.")
            selected_token = req.values[0]
            protected = voice_tts.global_protected_text_tokens()
            if selected_token not in protected:
                return EditorResponse.ephemeral("That protected token no longer exists.")
            page = _page_for_value(protected, selected_token)
            self._selection_state_for_message(session_message_id).protected_token = selected_token
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.PROTECTED, page=page),
                session_message_id=session_message_id,
                status="Selected protected token.",
            )

        if action.kind is VoiceAdminActionKind.ADD_PROTECTED:
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            await req.interaction.create_modal_response(
                "Add Protected Token",
                self._token_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._token_modal.rows(),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_PROTECTED:
            selected_token = self._selection_state_for_message(session_message_id).protected_token
            if selected_token is None:
                return EditorResponse.ephemeral("Choose a protected token first.")
            try:
                source_key, removed = voice_tts.remove_global_protected_text_token(selected_token)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            if removed:
                self._selection_state_for_message(session_message_id).protected_token = None
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=(
                    f"Removed protected token `{source_key}`."
                    if removed
                    else f"No protected token set for `{source_key}`."
                ),
            )

        if action.kind is VoiceAdminActionKind.SELECT_LINK_HOST:
            if not req.values:
                return EditorResponse.ephemeral("Choose a host label first.")
            selected_host = req.values[0]
            link_hosts = voice_tts.voice_link_host_labels()
            if selected_host not in link_hosts:
                return EditorResponse.ephemeral("That host label no longer exists.")
            page = _page_for_value(list(link_hosts), selected_host)
            self._selection_state_for_message(session_message_id).link_host = selected_host
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.LINKS,
                    links_view=VoiceAdminLinksView.HOSTS,
                    page=page,
                ),
                session_message_id=session_message_id,
                status="Selected link host label.",
            )

        if action.kind in {VoiceAdminActionKind.ADD_LINK_HOST, VoiceAdminActionKind.EDIT_LINK_HOST}:
            title = "Add Link Host Label"
            values = None
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            if action.kind is VoiceAdminActionKind.EDIT_LINK_HOST:
                host_key = self._selection_state_for_message(session_message_id).link_host
                if host_key is None:
                    return EditorResponse.ephemeral("Choose a host label first.")
                hosts = voice_tts.voice_link_host_labels()
                label_value = hosts.get(host_key)
                if label_value is None:
                    return EditorResponse.ephemeral("That host label no longer exists.")
                title = "Edit Link Host Label"
                values = {
                    _VOICE_ADMIN_HOST_FIELD_ID: host_key,
                    _VOICE_ADMIN_LABEL_FIELD_ID: label_value,
                }
            await req.interaction.create_modal_response(
                title,
                self._link_host_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._link_host_modal.rows(values),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_LINK_HOST:
            selected_host = self._selection_state_for_message(session_message_id).link_host
            if selected_host is None:
                return EditorResponse.ephemeral("Choose a host label first.")
            try:
                host_key, removed = voice_tts.remove_voice_link_host_label(selected_host)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            if removed:
                self._selection_state_for_message(session_message_id).link_host = None
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=(
                    f"Removed link host label `{host_key}`." if removed else f"No link host label set for `{host_key}`."
                ),
            )

        if action.kind is VoiceAdminActionKind.SELECT_LINK_RULE:
            if not req.values:
                return EditorResponse.ephemeral("Choose a link rule first.")
            if not req.values[0].isdigit():
                return EditorResponse.ephemeral("Link rule index is invalid.")
            selected_index = int(req.values[0])
            rule_numbers = [str(index) for index in range(1, len(voice_tts.voice_link_rules()) + 1)]
            if selected_index <= 0 or str(selected_index) not in rule_numbers:
                return EditorResponse.ephemeral("That link rule no longer exists.")
            page = _page_for_value(rule_numbers, str(selected_index))
            self._selection_state_for_message(session_message_id).link_rule_index = selected_index
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=self._state_with(
                    state,
                    section=VoiceAdminSection.LINKS,
                    links_view=VoiceAdminLinksView.RULES,
                    page=page,
                ),
                session_message_id=session_message_id,
                status="Selected link rule.",
            )

        if action.kind in {VoiceAdminActionKind.ADD_SIMPLE_LINK_RULE, VoiceAdminActionKind.ADD_COMPLEX_LINK_RULE}:
            mode = (
                VoiceLinkRuleMode.SIMPLE
                if action.kind is VoiceAdminActionKind.ADD_SIMPLE_LINK_RULE
                else VoiceLinkRuleMode.REGEX
            )
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            values = {
                _VOICE_ADMIN_TEMPLATE_FIELD_ID: "link {host} {title_norm}",
            }
            await req.interaction.create_modal_response(
                _voice_link_rule_modal_title(mode),
                self._link_rule_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._link_rule_modal.rows(values),
            )
            return None

        if action.kind is VoiceAdminActionKind.EDIT_LINK_RULE:
            modal_action = self._action_codec.build(
                action.kind,
                page=state.page,
                value=f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}",
            )
            index = self._selection_state_for_message(session_message_id).link_rule_index
            if index is None:
                return EditorResponse.ephemeral("Choose a link rule first.")
            rules = voice_tts.voice_link_rules()
            if index <= 0 or index > len(rules):
                return EditorResponse.ephemeral(f"index must be between 1 and {len(rules)}")
            rule = rules[index - 1]
            values = {
                _VOICE_ADMIN_LINK_RULE_URL_FIELD_ID: rule.example_url or "",
                _VOICE_ADMIN_PATH_REGEX_FIELD_ID: rule.input_pattern,
                _VOICE_ADMIN_TEMPLATE_FIELD_ID: rule.template,
            }
            await req.interaction.create_modal_response(
                "Edit Link Rule",
                self._link_rule_modal.build_id(modal_action, scope_id=actor_user_id, user_id=actor_user_id),
                components=self._link_rule_modal.rows(values),
            )
            return None

        if action.kind is VoiceAdminActionKind.REMOVE_LINK_RULE:
            selected_index = self._selection_state_for_message(session_message_id).link_rule_index
            if selected_index is None:
                return EditorResponse.ephemeral("Choose a link rule first.")
            try:
                rule_index, removed = voice_tts.remove_voice_link_rule(selected_index)
            except ValueError as xcp:
                return EditorResponse.ephemeral(str(xcp))
            self._selection_state_for_message(session_message_id).link_rule_index = None
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=req.locale,
                guild_id=guild_id,
                voice_tts=voice_tts,
                state=state,
                session_message_id=session_message_id,
                status=f"Removed link rule `{rule_index}`: `{removed.host}` | `{_voice_link_rule_value(removed)}`",
            )

        return EditorResponse.ephemeral("Unsupported voice admin action.")

    async def _on_model_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin model action.")
        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")

        url = req.values.get(_VOICE_ADMIN_MODEL_URL_FIELD_ID, "").strip()
        if not url:
            return EditorResponse.ephemeral("URL must not be empty.")

        actor_user_id = hikari.Snowflake(req.user_id)
        self._pending_model_scans.pop(actor_user_id, None)
        await req.interaction.edit_initial_response(
            content="Scanning Hugging Face for Piper-compatible models...",
            components=[],
        )

        try:
            repo_ref, candidates = await voice_tts.scan_piper_models_from_hf(url)
        except (LookupError, RuntimeError, ValueError) as xcp:
            return EditorResponse.ephemeral(str(xcp))

        if not candidates:
            return EditorResponse.ephemeral(
                f"No Piper-compatible models found in `{repo_ref.repo_id}` at `{repo_ref.revision}`."
            )
        if len(candidates) > 25:
            return EditorResponse.ephemeral(
                "\n".join(
                    [
                        f"Found `{len(candidates)}` model files in `{repo_ref.repo_id}`.",
                        "Discord select menus support up to 25 options.",
                        "Use a direct `.onnx` file URL here to choose one explicitly.",
                    ]
                )
            )
        if len(candidates) > 1:
            self._pending_model_scans[actor_user_id] = PendingModelScan(
                repo_ref=repo_ref,
                candidates=tuple(candidates),
            )
            return await self._build_editor_response(
                actor_user_id=actor_user_id,
                locale=self._editor.resolve_locale(req.interaction),
                guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
                voice_tts=voice_tts,
                state=self._state_with(state, section=VoiceAdminSection.MODELS, page=0),
                status=f"Choose one of `{len(candidates)}` scanned model files from `{repo_ref.repo_id}`.",
            )

        await req.interaction.edit_initial_response(
            content=(f"Downloading `{Path(candidates[0]).name}` from `{repo_ref.repo_id}` ({repo_ref.revision})..."),
            components=[],
        )

        try:
            model_name, has_config = await voice_tts.add_piper_model_from_hf(repo_ref, candidates[0])
        except FileExistsError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        except (LookupError, RuntimeError, ValueError) as xcp:
            return EditorResponse.ephemeral(str(xcp))
        except requests.RequestException as xcp:
            return EditorResponse.ephemeral(f"Failed to download model: {xcp}")

        self._pending_model_scans.pop(actor_user_id, None)
        custom_models = voice_tts.available_custom_voices()
        page = _page_for_value(custom_models, model_name) if model_name in custom_models else 0
        return await self._build_editor_response(
            actor_user_id=actor_user_id,
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(state, section=VoiceAdminSection.MODELS, page=page),
            status=(
                f"Added TTS model `{model_name}` "
                f"(config: `{'downloaded' if has_config else 'not found'}`). "
                f"Use `{_VOICE_SETTINGS_COMMAND}` to switch to it."
            ),
            force_refresh=True,
        )

    async def _on_substitution_modal_submit(
        self, req: ModalRequest, deps: Mapping[str, object]
    ) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin substitution action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        source = _normalise_voice_source(req.values.get(_VOICE_ADMIN_SOURCE_FIELD_ID, ""))
        target = req.values.get(_VOICE_ADMIN_TARGET_FIELD_ID, "").strip()
        if not source.key:
            return EditorResponse.ephemeral("Source must not be empty.")
        if not target:
            return EditorResponse.ephemeral("Target must not be empty.")
        if action.kind not in {VoiceAdminActionKind.ADD_SUBSTITUTION, VoiceAdminActionKind.EDIT_SUBSTITUTION}:
            return EditorResponse.ephemeral("Unsupported voice admin substitution action.")

        try:
            case_sensitive = _parse_case_sensitive_value(req.values.get(_VOICE_ADMIN_CASE_SENSITIVE_FIELD_ID, ""))
            category_key, source_key, replacement, existed = voice_tts.set_global_text_substitution(
                state.substitution_category.value,
                source.key,
                target,
                case_sensitive=case_sensitive,
            )
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        previous_key = self._selection_state_for_message(session_message_id).substitution_source
        if action.kind is VoiceAdminActionKind.EDIT_SUBSTITUTION and previous_key and previous_key != source_key:
            voice_tts.remove_global_text_substitution(state.substitution_category.value, previous_key)

        entries = list(voice_tts.global_text_substitutions(state.substitution_category.value))
        page = _page_for_value(entries, source_key)
        self._selection_state_for_message(session_message_id).substitution_source = source_key
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(state, section=VoiceAdminSection.SUBSTITUTIONS, page=page),
            session_message_id=session_message_id,
            status=(
                f"{'Updated' if existed else 'Added'} global {category_key}: "
                f"`{_substitution_rule_label(source_key, replacement)}` -> `{replacement.target}`."
            ),
        )

    async def _on_mention_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin mention action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        target = req.values.get(_VOICE_ADMIN_TARGET_FIELD_ID, "").strip()
        if not target:
            return EditorResponse.ephemeral("Spoken name must not be empty.")
        if action.kind not in {VoiceAdminActionKind.ADD_MENTION_OVERRIDE, VoiceAdminActionKind.EDIT_MENTION_OVERRIDE}:
            return EditorResponse.ephemeral("Unsupported voice admin mention action.")

        try:
            selected_user_id = self._selection_state_for_message(session_message_id).mention_target_user_id
            if selected_user_id is not None:
                target_user_id = hikari.Snowflake(selected_user_id)
            else:
                target_user_id = _parse_mention_override_target(req.values.get(_VOICE_ADMIN_MENTION_USER_FIELD_ID, ""))
            resolved_user_id, spoken_name, existed = voice_tts.set_global_mention_override(target_user_id, target)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        page = _page_for_value(
            [str(target_uid) for target_uid in voice_tts.global_mention_overrides()],
            str(resolved_user_id),
        )
        self._selection_state_for_message(session_message_id).mention_target_user_id = int(resolved_user_id)
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(state, section=VoiceAdminSection.MENTIONS, page=page),
            session_message_id=session_message_id,
            status=(
                f"{'Updated' if existed else 'Added'} shared mention override `{resolved_user_id}` -> `{spoken_name}`."
            ),
        )

    async def _on_entry_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin entry action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra) if extra is not None else None

        source = _normalise_voice_source(req.values.get(_VOICE_ADMIN_SOURCE_FIELD_ID, ""))
        target = req.values.get(_VOICE_ADMIN_TARGET_FIELD_ID, "").strip()
        if not source.key:
            return EditorResponse.ephemeral("Source must not be empty.")
        if not target:
            return EditorResponse.ephemeral("Target must not be empty.")

        if action.kind is not VoiceAdminActionKind.OPEN_PRONUNCIATION_MODAL:
            return EditorResponse.ephemeral("Unsupported voice admin entry action.")

        pending = self._pending_pronunciations.get(hikari.Snowflake(req.user_id))
        if pending is None or pending.voice is None:
            return EditorResponse.ephemeral("Choose a voice first.")
        try:
            voice_key, source_key, replacement, existed = voice_tts.set_global_pronunciation(
                pending.voice,
                source.key,
                target,
                pending.format,
            )
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))
        entries = [
            _voice_pronunciation_value(entry_voice, entry_source)
            for entry_voice, voice_entries in sorted(voice_tts.all_global_pronunciations().items())
            for entry_source in voice_entries
        ]
        selected_value = _voice_pronunciation_value(voice_key, source_key)
        page = _page_for_value(entries, selected_value)
        if session_message_id is not None:
            self._selection_state_for_message(session_message_id).pronunciation_value = selected_value
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(
                state,
                section=VoiceAdminSection.PRONUNCIATIONS,
                page=page,
                pronunciation_view=VoiceAdminPronunciationView.LIST,
            ),
            session_message_id=session_message_id,
            status=(
                f"{'Updated' if existed else 'Added'} global pronunciation `{voice_key}` / "
                f"`{source_key}` -> `{_pronunciation_override_display(replacement)}`."
            ),
        )

    async def _on_token_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin protected-token action.")
        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        token = req.values.get(_VOICE_ADMIN_SOURCE_FIELD_ID, "").strip()
        if not token:
            return EditorResponse.ephemeral("Token must not be empty.")

        try:
            source_key, existed = voice_tts.add_global_protected_text_token(token)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        tokens = voice_tts.global_protected_text_tokens()
        page = _page_for_value(tokens, source_key)
        self._selection_state_for_message(session_message_id).protected_token = source_key
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(state, section=VoiceAdminSection.PROTECTED, page=page),
            session_message_id=session_message_id,
            status=(f"Already protected: `{source_key}`" if existed else f"Added protected token `{source_key}`."),
        )

    async def _on_pronunciation_modal_submit(
        self,
        req: ModalRequest,
        deps: Mapping[str, object],
    ) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin pronunciation action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        voice = req.values.get(_VOICE_ADMIN_PRONUNCIATION_VOICE_FIELD_ID, "").strip()
        source = _normalise_voice_source(req.values.get(_VOICE_ADMIN_SOURCE_FIELD_ID, ""))
        target = req.values.get(_VOICE_ADMIN_TARGET_FIELD_ID, "").strip()
        format_raw = req.values.get(_VOICE_ADMIN_PRONUNCIATION_FORMAT_FIELD_ID, "").strip()
        if not voice:
            return EditorResponse.ephemeral("Voice must not be empty.")
        if not source.key:
            return EditorResponse.ephemeral("Source must not be empty.")
        if not target:
            return EditorResponse.ephemeral("Target must not be empty.")

        try:
            voice_key, source_key, replacement, existed = voice_tts.set_global_pronunciation(
                voice,
                source.key,
                target,
                format_raw,
            )
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        previous_value = self._selection_state_for_message(session_message_id).pronunciation_value
        if action.kind is VoiceAdminActionKind.EDIT_PRONUNCIATION and previous_value:
            previous = _parse_voice_pronunciation_value(previous_value)
            if previous is not None and previous != (voice_key, source_key):
                voice_tts.remove_global_pronunciation(*previous)

        entries = [
            _voice_pronunciation_value(entry_voice, entry_source)
            for entry_voice, voice_entries in sorted(voice_tts.all_global_pronunciations().items())
            for entry_source in voice_entries
        ]
        page = _page_for_value(entries, _voice_pronunciation_value(voice_key, source_key))
        self._selection_state_for_message(session_message_id).pronunciation_value = _voice_pronunciation_value(
            voice_key,
            source_key,
        )
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(state, section=VoiceAdminSection.PRONUNCIATIONS, page=page),
            session_message_id=session_message_id,
            status=(
                f"{'Updated' if existed else 'Added'} global pronunciation `{voice_key}` / "
                f"`{source_key}` -> `{_pronunciation_override_display(replacement)}`."
            ),
        )

    async def _on_link_host_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin link-host action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        host = req.values.get(_VOICE_ADMIN_HOST_FIELD_ID, "").strip()
        label = req.values.get(_VOICE_ADMIN_LABEL_FIELD_ID, "").strip()
        if not host:
            return EditorResponse.ephemeral("Host must not be empty.")
        if not label:
            return EditorResponse.ephemeral("Label must not be empty.")

        if action.kind not in {VoiceAdminActionKind.ADD_LINK_HOST, VoiceAdminActionKind.EDIT_LINK_HOST}:
            return EditorResponse.ephemeral("Unsupported voice admin link-host action.")

        try:
            host_key, label_value, existed = voice_tts.set_voice_link_host_label(host, label)
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        previous_host = self._selection_state_for_message(session_message_id).link_host
        if action.kind is VoiceAdminActionKind.EDIT_LINK_HOST and previous_host and previous_host != host_key:
            voice_tts.remove_voice_link_host_label(previous_host)

        hosts = list(voice_tts.voice_link_host_labels())
        page = _page_for_value(hosts, host_key)
        self._selection_state_for_message(session_message_id).link_host = host_key
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(
                state,
                section=VoiceAdminSection.LINKS,
                links_view=VoiceAdminLinksView.HOSTS,
                page=page,
            ),
            session_message_id=session_message_id,
            status=f"{'Updated' if existed else 'Added'} link host label: `{host_key}` -> `{label_value}`.",
        )

    async def _on_link_rule_modal_submit(self, req: ModalRequest, deps: Mapping[str, object]) -> EditorResponse | None:
        voice_tts = self._require_voice_tts(deps)
        action = self._action_codec.parse(req.action)
        if action is None:
            return EditorResponse.ephemeral("Unknown voice admin link-rule action.")

        state, extra = self._state_and_extra_from_value(action)
        if state is None:
            return EditorResponse.ephemeral("Voice admin state is invalid.")
        session_message_id = self._selection_message_id_from_extra(extra)

        example_url = req.values.get(_VOICE_ADMIN_LINK_RULE_URL_FIELD_ID, "").strip()
        path_regex = req.values.get(_VOICE_ADMIN_PATH_REGEX_FIELD_ID, "").strip()
        template = req.values.get(_VOICE_ADMIN_TEMPLATE_FIELD_ID, "").strip()
        if not example_url:
            return EditorResponse.ephemeral("Example URL must not be empty.")
        if not template:
            return EditorResponse.ephemeral("Speak As must not be empty.")

        try:
            if action.kind is VoiceAdminActionKind.ADD_SIMPLE_LINK_RULE:
                rule_index, rule = voice_tts.add_voice_link_rule(
                    "",
                    path_regex,
                    template,
                    mode=VoiceLinkRuleMode.SIMPLE,
                    example_url=example_url,
                )
                existed = False
            elif action.kind is VoiceAdminActionKind.ADD_COMPLEX_LINK_RULE:
                rule_index, rule = voice_tts.add_voice_link_rule(
                    "",
                    path_regex,
                    template,
                    mode=VoiceLinkRuleMode.REGEX,
                    example_url=example_url,
                )
                existed = False
            elif action.kind is VoiceAdminActionKind.EDIT_LINK_RULE:
                previous_index = self._selection_state_for_message(session_message_id).link_rule_index
                if previous_index is None:
                    return EditorResponse.ephemeral("Link rule index is invalid.")
                rules = voice_tts.voice_link_rules()
                if previous_index <= 0 or previous_index > len(rules):
                    return EditorResponse.ephemeral(f"index must be between 1 and {len(rules)}")
                existing_rule = rules[previous_index - 1]
                rule_index, rule = voice_tts.update_voice_link_rule(
                    previous_index,
                    host="" if example_url else existing_rule.host,
                    path_regex=path_regex,
                    template=template,
                    mode=existing_rule.mode,
                    example_url=example_url,
                )
                existed = True
            else:
                return EditorResponse.ephemeral("Unsupported voice admin link-rule action.")
        except ValueError as xcp:
            return EditorResponse.ephemeral(str(xcp))

        rule_numbers = [str(index) for index in range(1, len(voice_tts.voice_link_rules()) + 1)]
        page = _page_for_value(rule_numbers, str(rule_index))
        self._selection_state_for_message(session_message_id).link_rule_index = rule_index
        preview = voice_tts.preview_voice_link_rule(rule)
        status = f"{'Updated' if existed else 'Added'} link rule `{rule_index}`: `{rule.host}` | `{_voice_link_rule_value(rule)}`"
        if preview is not None:
            status += f" | preview: `{preview}`"
        return await self._build_editor_response(
            actor_user_id=hikari.Snowflake(req.user_id),
            locale=self._editor.resolve_locale(req.interaction),
            guild_id=req.interaction.guild_id if req.interaction.guild_id is not None else None,
            voice_tts=voice_tts,
            state=self._state_with(
                state,
                section=VoiceAdminSection.LINKS,
                links_view=VoiceAdminLinksView.RULES,
                page=page,
            ),
            session_message_id=session_message_id,
            status=status,
        )

    async def _build_editor_response(
        self,
        *,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        guild_id: hikari.Snowflake | None = None,
        voice_tts: VoiceTTSService,
        state: VoiceAdminState,
        session_message_id: hikari.Snowflake | None = None,
        status: str,
        force_refresh: bool = False,
    ) -> EditorResponse:
        embed, components = await self._render_editor(
            actor_user_id=actor_user_id,
            locale=locale,
            guild_id=guild_id,
            voice_tts=voice_tts,
            state=state,
            session_message_id=session_message_id,
            force_refresh=force_refresh,
        )
        return EditorResponse.update(status, components=components, embeds=[embed])

    async def _render_editor(
        self,
        *,
        actor_user_id: hikari.Snowflake,
        locale: hikari.Locale,
        guild_id: hikari.Snowflake | None = None,
        voice_tts: VoiceTTSService,
        state: VoiceAdminState,
        session_message_id: hikari.Snowflake | None = None,
        force_refresh: bool = False,
    ) -> tuple[hikari.Embed, list[hikari.api.MessageActionRowBuilder]]:
        custom_models = voice_tts.available_custom_voices()
        voices = await voice_tts.available_voices(force_refresh=force_refresh)
        mention_overrides = voice_tts.global_mention_overrides()
        substitutions = voice_tts.global_text_substitutions(state.substitution_category.value)
        global_pronunciations = voice_tts.all_global_pronunciations()
        flattened_pronunciations = [
            (voice, source, entry)
            for voice, entries in sorted(global_pronunciations.items())
            for source, entry in entries.items()
        ]
        protected = voice_tts.global_protected_text_tokens()
        link_hosts = voice_tts.voice_link_host_labels()
        link_rules = voice_tts.voice_link_rules()
        selection_state = self._selection_state.get(session_message_id) if session_message_id is not None else None
        current_target = voice_tts.voice_target(guild_id) if guild_id is not None else None

        embed = hikari.Embed(
            title="Voice Admin",
            description="Manage shared TTS configuration.",
            colour=0x7A4B32,
        )
        editor_ctx = self._editor.context(scope_id=actor_user_id, user_id=actor_user_id, locale=locale)
        layout = EditorLayout(editor_ctx)
        if not (
            state.section is VoiceAdminSection.CHANNELS
            and state.channels_view is VoiceAdminChannelsView.CONFIG
        ):
            self._add_section_selector(layout=layout, state=state)

        if state.section is VoiceAdminSection.OVERVIEW:
            embed.add_field(
                name="Runtime",
                value=_display_value(
                    [
                        f"engine: {voice_tts._engine_display()}",
                        f"default voice: {voice_tts.voice}",
                        f"default variant: {voice_tts.variant or 'none'}",
                        f"available voices: {len(voices)}",
                        f"custom models: {len(custom_models)}",
                    ]
                ),
                inline=False,
            )
            embed.add_field(
                name="Shared Corrections",
                value=_display_value(
                    [
                        f"slang substitutions: {len(voice_tts.global_text_substitutions('slang'))}",
                        f"typo substitutions: {len(voice_tts.global_text_substitutions('typo'))}",
                        f"mention overrides: {len(mention_overrides)}",
                        f"voice pronunciations: {len(flattened_pronunciations)}",
                        f"protected tokens: {len(protected)}",
                    ]
                ),
                inline=False,
            )
            embed.add_field(
                name="Links",
                value=_display_value(
                    [
                        f"host labels: {len(link_hosts)}",
                        f"regex rules: {len(link_rules)}",
                    ]
                ),
                inline=False,
            )
            embed.add_field(
                name="Guild Channels",
                value=_display_value(
                    [
                        f"guild: `{guild_id}`" if guild_id is not None else "guild: not opened from a server",
                        (
                            f"voice: <#{int(current_target.voice_channel)}>"
                            if current_target is not None
                            else "voice: not configured"
                        ),
                        (
                            f"primary tts: <#{int(current_target.primary_tts_channel)}>"
                            if current_target is not None
                            else "primary tts: not configured"
                        ),
                        (
                            f"secondary tts: <#{int(current_target.secondary_tts_channel)}>"
                            if current_target is not None and current_target.secondary_tts_channel is not None
                            else "secondary tts: not configured"
                        ),
                        (
                            f"relay tts: {'enabled' if current_target.relay_tts_enabled else 'disabled'}"
                            if current_target is not None
                            else "relay tts: disabled"
                        ),
                    ]
                    if guild_id is not None
                    else ["Open this editor in a server to manage the current guild channels."]
                ),
                inline=False,
            )
            layout.page_footer(
                self._action_codec.build(VoiceAdminActionKind.CLOSE, page=0),
                page_state=EditorPageState(page=0, total_pages=1),
                extra_buttons=(
                    EditorButton(
                        self._build_state_action(VoiceAdminActionKind.SHOW_CHANNELS, state),
                        "Manage Channels",
                        style=hikari.ButtonStyle.PRIMARY,
                        is_disabled=guild_id is None,
                    ),
                    EditorButton(self._build_state_action(VoiceAdminActionKind.REFRESH, state), "Refresh"),
                ),
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.CHANNELS:
            pending_voice_channel_id = (
                hikari.Snowflake(selection_state.pending_voice_channel_id)
                if selection_state is not None and selection_state.pending_voice_channel_id is not None
                else current_target.voice_channel if current_target is not None else None
            )
            pending_primary_tts_channel_id = (
                hikari.Snowflake(selection_state.pending_primary_tts_channel_id)
                if selection_state is not None and selection_state.pending_primary_tts_channel_id is not None
                else current_target.primary_tts_channel if current_target is not None else None
            )
            pending_secondary_tts_channel_id = (
                hikari.Snowflake(selection_state.pending_secondary_tts_channel_id)
                if selection_state is not None and selection_state.pending_secondary_tts_channel_id is not None
                else current_target.secondary_tts_channel if current_target is not None else None
            )
            primary_tts_listen_enabled = current_target.primary_tts_listen_enabled if current_target is not None else False
            secondary_tts_listen_enabled = (
                current_target.secondary_tts_listen_enabled if current_target is not None else False
            )
            embed.add_field(
                name="Current Guild Channels",
                value=_display_value(
                    [
                        f"guild: `{guild_id}`" if guild_id is not None else "guild: not opened from a server",
                        f"voice channel: {_channel_reference(pending_voice_channel_id, missing='not selected')}",
                        (
                            "primary tts channel: "
                            f"{_channel_reference(pending_primary_tts_channel_id, missing='not selected')} "
                            f"({'listening' if primary_tts_listen_enabled else 'muted'})"
                        ),
                        "secondary tts channel: "
                        f"{_channel_reference(pending_secondary_tts_channel_id, missing='not selected')} "
                        f"({'listening' if secondary_tts_listen_enabled else 'muted'})",
                        f"relay tts: {'enabled' if current_target and current_target.relay_tts_enabled else 'disabled'}",
                    ]
                ),
                inline=False,
            )
            if state.channels_view is VoiceAdminChannelsView.SUMMARY:
                embed.add_field(
                    name="Channels",
                    value=_display_value(
                        [
                            "Open channel config to choose or clear the voice, primary TTS, and secondary TTS channels."
                            if guild_id is not None
                            else "Open this editor in a server to manage channels.",
                            "Relay TTS only applies when a game relay posts into a configured TTS channel and the linked Discord user has Listen On."
                            if guild_id is not None
                            else "Relay TTS is unavailable outside a server context.",
                        ]
                    ),
                    inline=False,
                )
                if guild_id is not None:
                    layout.add_buttons(
                        EditorButton(
                            self._build_state_action(VoiceAdminActionKind.SHOW_CHANNEL_CONFIG, state),
                            "Channel Config",
                            style=hikari.ButtonStyle.PRIMARY,
                        ),
                        EditorButton(
                            self._build_state_action(VoiceAdminActionKind.TOGGLE_PRIMARY_TTS_LISTENING, state),
                            "Primary On" if primary_tts_listen_enabled else "Primary Off",
                            style=(
                                hikari.ButtonStyle.SUCCESS if primary_tts_listen_enabled else hikari.ButtonStyle.SECONDARY
                            ),
                            is_disabled=current_target is None,
                        ),
                        EditorButton(
                            self._build_state_action(VoiceAdminActionKind.TOGGLE_SECONDARY_TTS_LISTENING, state),
                            "Secondary On" if secondary_tts_listen_enabled else "Secondary Off",
                            style=(
                                hikari.ButtonStyle.SUCCESS if secondary_tts_listen_enabled else hikari.ButtonStyle.SECONDARY
                            ),
                            is_disabled=current_target is None or current_target.secondary_tts_channel is None,
                        ),
                        EditorButton(
                            self._build_state_action(VoiceAdminActionKind.TOGGLE_RELAY_TTS, state),
                            "Relay TTS On" if current_target and current_target.relay_tts_enabled else "Relay TTS Off",
                            style=(
                                hikari.ButtonStyle.SUCCESS
                                if current_target and current_target.relay_tts_enabled
                                else hikari.ButtonStyle.DANGER
                            ),
                            is_disabled=current_target is None,
                        ),
                    )
                layout.page_footer(
                    self._action_codec.build(VoiceAdminActionKind.CLOSE, page=0),
                    page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
                    back_action=self._build_state_action(
                        VoiceAdminActionKind.SHOW_OVERVIEW,
                        self._state_with(state, section=VoiceAdminSection.OVERVIEW, page=0),
                    ),
                    extra_buttons=(EditorButton(self._build_state_action(VoiceAdminActionKind.REFRESH, state), "Refresh"),),
                )
                return embed, layout.build()

            embed.add_field(
                name="Channel Config",
                value=_display_value(
                    [
                        "Choose channels below. Clearing voice or primary TTS removes the saved voice config for this guild."
                        if guild_id is not None
                        else "Open this editor in a server to configure channels.",
                        "Secondary TTS is optional and can be cleared independently."
                        if guild_id is not None
                        else "Secondary TTS is optional when configured from a server.",
                    ]
                ),
                inline=False,
            )
            if guild_id is not None:
                layout.add_channel_select(
                    self._build_state_action(VoiceAdminActionKind.SELECT_GUILD_VOICE_CHANNEL, state),
                    channel_types=_VOICE_ADMIN_VOICE_CHANNEL_TYPES,
                    placeholder="Choose the current guild voice channel",
                )
                layout.add_channel_select(
                    self._build_state_action(
                        VoiceAdminActionKind.SELECT_GUILD_PRIMARY_TTS_CHANNEL,
                        self._state_with(state, channels_view=VoiceAdminChannelsView.CONFIG),
                    ),
                    channel_types=_VOICE_ADMIN_TTS_CHANNEL_TYPES,
                    placeholder="Choose the primary guild TTS channel",
                )
                layout.add_channel_select(
                    self._build_state_action(
                        VoiceAdminActionKind.SELECT_GUILD_SECONDARY_TTS_CHANNEL,
                        self._state_with(
                            state,
                            channels_view=VoiceAdminChannelsView.CONFIG,
                            page=0,
                        ),
                    ),
                    channel_types=_VOICE_ADMIN_TTS_CHANNEL_TYPES,
                    placeholder="Choose the secondary guild TTS channel",
                )
                layout.next_row().add_buttons(
                    EditorButton(
                        self._build_state_action(VoiceAdminActionKind.CLEAR_GUILD_VOICE_CHANNEL, state),
                        "Clear Voice",
                        style=hikari.ButtonStyle.DANGER,
                        is_disabled=pending_voice_channel_id is None and current_target is None,
                    ),
                    EditorButton(
                        self._build_state_action(VoiceAdminActionKind.CLEAR_PRIMARY_TTS_CHANNEL, state),
                        "Clear Primary",
                        style=hikari.ButtonStyle.DANGER,
                        is_disabled=pending_primary_tts_channel_id is None and current_target is None,
                    ),
                    EditorButton(
                        self._build_state_action(VoiceAdminActionKind.CLEAR_SECONDARY_TTS_CHANNEL, state),
                        "Clear Secondary",
                        style=hikari.ButtonStyle.SECONDARY,
                        is_disabled=pending_secondary_tts_channel_id is None,
                    ),
                )
            layout.page_footer(
                self._action_codec.build(VoiceAdminActionKind.CLOSE, page=0),
                page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
                back_action=self._build_state_action(
                    VoiceAdminActionKind.SHOW_CHANNELS,
                    self._state_with(state, channels_view=VoiceAdminChannelsView.SUMMARY, page=0),
                ),
                extra_buttons=(EditorButton(self._build_state_action(VoiceAdminActionKind.REFRESH, state), "Refresh"),),
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.MODELS:
            paged = _paginate(custom_models, state.page)
            pending_scan = self._pending_model_scans.get(actor_user_id)
            builtin_voices = _builtin_voice_names(voices, custom_models)
            embed.add_field(
                name="Models",
                value=_display_value(
                    [
                        f"default voice: {voice_tts.voice}",
                        f"default variant: {voice_tts.variant or 'none'}",
                        f"available voices: {len(voices)}",
                        f"built-in voices: {len(builtin_voices)}",
                        f"custom models: {len(custom_models)}",
                        (
                            f"pending scan: {pending_scan.repo_ref.repo_id} ({len(pending_scan.candidates)} files)"
                            if pending_scan is not None
                            else "pending scan: none"
                        ),
                    ]
                ),
                inline=False,
            )
            if builtin_voices:
                layout.add_text_select(
                    self._action_codec.build(
                        VoiceAdminActionKind.SET_DEFAULT_VOICE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    options=[
                        EditorSelectOption(
                            label=_component_text(voice),
                            value=voice,
                            description=(
                                "Current default built-in voice"
                                if voice.lower() == voice_tts.voice.lower()
                                else "Set bot default to this built-in voice"
                            ),
                            is_default=voice.lower() == voice_tts.voice.lower(),
                        )
                        for voice in builtin_voices
                    ],
                    placeholder="Choose the default built-in voice",
                )
            if pending_scan is not None:
                layout.add_text_select(
                    self._action_codec.build(
                        VoiceAdminActionKind.SELECT_MODEL_CANDIDATE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    options=[
                        EditorSelectOption(
                            label=_component_text(Path(path).name),
                            value=path,
                            description=_component_text(path),
                        )
                        for path in pending_scan.candidates
                    ],
                    placeholder="Choose a scanned model file to download",
                )
            if paged.visible:
                layout.add_text_select(
                    self._action_codec.build(
                        VoiceAdminActionKind.REMOVE_MODEL,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    options=[
                        EditorSelectOption(
                            label=_component_text(model),
                            value=model,
                            description="Delete this custom model",
                        )
                        for model in paged.visible
                    ],
                    placeholder="Delete a custom voice model",
                )
            self._add_page_footer(
                layout=layout,
                state=state,
                page_state=paged.page_state,
                extra_buttons=(
                    EditorButton(
                        self._action_codec.build(
                            VoiceAdminActionKind.ADD_MODEL,
                            page=paged.page_state.page,
                            value=_voice_admin_state_value(state),
                        ),
                        "Add Voice",
                        style=hikari.ButtonStyle.PRIMARY,
                    ),
                ),
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.MENTIONS:
            self._render_mention_overrides_section(
                embed=embed,
                layout=layout,
                state=state,
                mention_overrides=mention_overrides,
                selected_user_id=selection_state.mention_target_user_id if selection_state is not None else None,
                voice_tts=voice_tts,
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.SUBSTITUTIONS:
            self._add_substitution_category_selector(layout=layout, state=state)
            self._render_selected_mapping_section(
                embed=embed,
                layout=layout,
                state=state,
                items=substitutions,
                section_title="Shared Substitutions",
                summary_lines=(
                    f"category: {state.substitution_category.label}",
                    f"protected tokens: {len(protected)}",
                ),
                selected_key=selection_state.substitution_source if selection_state is not None else None,
                select_action=VoiceAdminActionKind.SELECT_SUBSTITUTION,
                add_action=VoiceAdminActionKind.ADD_SUBSTITUTION,
                edit_action=VoiceAdminActionKind.EDIT_SUBSTITUTION,
                remove_action=VoiceAdminActionKind.REMOVE_SUBSTITUTION,
                item_description=lambda value: _substitution_rule_value(value),
                item_label=lambda source, _value: source,
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.PRONUNCIATIONS:
            if state.pronunciation_view is VoiceAdminPronunciationView.CREATE:
                pending = self._pending_pronunciations.get(actor_user_id)
                if pending is None:
                    pending = PendingGlobalPronunciation(voice=voice_tts.voice, format=PronunciationFormat.TEXT)
                    self._pending_pronunciations[actor_user_id] = pending
                embed.add_field(
                    name="Add Global Pronunciation",
                    value=_display_value(
                        [
                            f"voice: {pending.voice or 'not selected'}",
                            f"format: {pending.format.value}",
                            "Choose the voice and format, then open the source/target modal.",
                        ]
                    ),
                    inline=False,
                )
                layout.add_text_select(
                    self._action_codec.build(
                        VoiceAdminActionKind.SET_PRONUNCIATION_VOICE,
                        page=0,
                        value=_voice_admin_state_value(state),
                    ),
                    options=[
                        EditorSelectOption(
                            label=_component_text(voice),
                            value=voice,
                            description="Selected voice" if pending.voice == voice else "Use this voice",
                            is_default=pending.voice == voice,
                        )
                        for voice in voices
                    ],
                    placeholder="Choose a voice",
                )
                layout.add_text_select(
                    self._action_codec.build(
                        VoiceAdminActionKind.SET_PRONUNCIATION_FORMAT,
                        page=0,
                        value=_voice_admin_state_value(state),
                    ),
                    options=[
                        EditorSelectOption(
                            label="Text",
                            value=PronunciationFormat.TEXT.value,
                            description="Use plain text speech",
                            is_default=pending.format is PronunciationFormat.TEXT,
                        ),
                        EditorSelectOption(
                            label="IPA",
                            value=PronunciationFormat.IPA.value,
                            description="Use IPA pronunciation",
                            is_default=pending.format is PronunciationFormat.IPA,
                        ),
                    ],
                    placeholder="Choose a format",
                )
                layout.page_footer(
                    self._action_codec.build(VoiceAdminActionKind.CLOSE, page=0),
                    page_state=EditorPageState(page=0, total_pages=1, is_subpage=True),
                    back_action=self._build_state_action(
                        VoiceAdminActionKind.SHOW_SECTION,
                        self._state_with(
                            state,
                            section=VoiceAdminSection.PRONUNCIATIONS,
                            pronunciation_view=VoiceAdminPronunciationView.LIST,
                            page=0,
                        ),
                    ),
                    extra_buttons=(
                        EditorButton(
                            self._action_codec.build(
                                VoiceAdminActionKind.OPEN_PRONUNCIATION_MODAL,
                                page=0,
                                value=(
                                    _voice_admin_state_value(state)
                                    if session_message_id is None
                                    else f"{_voice_admin_state_value(state)}{_VOICE_ADMIN_STATE_VALUE_SEPARATOR}{int(session_message_id)}"
                                ),
                            ),
                            "Set Source/Target",
                            style=hikari.ButtonStyle.PRIMARY,
                            is_disabled=pending.voice is None,
                        ),
                        EditorButton(self._build_state_action(VoiceAdminActionKind.REFRESH, state), "Refresh"),
                    ),
                )
                return embed, layout.build()

            self._render_global_pronunciations_section(
                embed=embed,
                layout=layout,
                state=state,
                flattened_pronunciations=flattened_pronunciations,
                global_pronunciations=global_pronunciations,
                selected_value=selection_state.pronunciation_value if selection_state is not None else None,
                voice_tts=voice_tts,
            )
            return embed, layout.build()

        if state.section is VoiceAdminSection.PROTECTED:
            self._render_selected_mapping_section(
                embed=embed,
                layout=layout,
                state=state,
                items={token: token for token in protected},
                section_title="Protected Tokens",
                summary_lines=(f"shared substitutions: {len(voice_tts.base_text_substitutions())}",),
                selected_key=selection_state.protected_token if selection_state is not None else None,
                select_action=VoiceAdminActionKind.SELECT_PROTECTED,
                add_action=VoiceAdminActionKind.ADD_PROTECTED,
                edit_action=None,
                remove_action=VoiceAdminActionKind.REMOVE_PROTECTED,
                add_label="Add Token",
                item_description=lambda _value: "Protected from shared substitutions and typo fixes.",
            )
            return embed, layout.build()

        self._add_links_view_selector(layout=layout, state=state)
        if state.links_view is VoiceAdminLinksView.HOSTS:
            self._render_selected_mapping_section(
                embed=embed,
                layout=layout,
                state=state,
                items=link_hosts,
                section_title="Link Host Labels",
                summary_lines=(
                    f"view: {state.links_view.label}",
                    f"link rules: {len(link_rules)}",
                ),
                selected_key=selection_state.link_host if selection_state is not None else None,
                select_action=VoiceAdminActionKind.SELECT_LINK_HOST,
                add_action=VoiceAdminActionKind.ADD_LINK_HOST,
                edit_action=VoiceAdminActionKind.EDIT_LINK_HOST,
                remove_action=VoiceAdminActionKind.REMOVE_LINK_HOST,
                item_description=lambda value: value,
            )
            return embed, layout.build()

        self._render_link_rules_section(
            embed=embed,
            layout=layout,
            state=state,
            link_rules=link_rules,
            selected_index=selection_state.link_rule_index if selection_state is not None else None,
            voice_tts=voice_tts,
        )
        return embed, layout.build()

    def _render_mention_overrides_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceAdminState,
        mention_overrides: dict[int, str],
        selected_user_id: int | None,
        voice_tts: VoiceTTSService,
    ) -> None:
        override_items = list(mention_overrides.items())
        paged = _paginate(override_items, state.page)
        selected_override = mention_overrides.get(selected_user_id) if selected_user_id is not None else None

        embed.add_field(
            name="Shared Mention Overrides",
            value=_display_value(
                (
                    f"entries: {len(mention_overrides)}",
                    "Used when a mention resolves to the user's username.",
                    (
                        f"selected: {_mention_override_label(voice_tts, selected_user_id)}"
                        if selected_user_id is not None
                        else "selected: none"
                    ),
                    f"override: {selected_override or 'none'}",
                )
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Current Page ({paged.total_count})",
            value=_display_value(
                [
                    f"{_mention_override_label(voice_tts, target_user_id)}: {spoken_name}"
                    for target_user_id, spoken_name in paged.visible
                ]
            ),
            inline=False,
        )

        layout.add_user_select(
            self._build_state_action(VoiceAdminActionKind.SELECT_MENTION_TARGET, state),
            placeholder="Choose a user to edit or remove",
        )
        self._add_page_footer(
            layout=layout,
            state=state,
            page_state=paged.page_state,
            extra_buttons=(
                EditorButton(
                    self._build_state_action(VoiceAdminActionKind.EDIT_MENTION_OVERRIDE, state),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_user_id is None,
                ),
                EditorButton(
                    self._build_state_action(VoiceAdminActionKind.REMOVE_MENTION_OVERRIDE, state),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_user_id is None or selected_override is None,
                ),
            ),
        )

    def _render_selected_mapping_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceAdminState,
        items: dict[str, ValueT],
        section_title: str,
        summary_lines: Sequence[str],
        selected_key: str | None,
        select_action: VoiceAdminActionKind,
        add_action: VoiceAdminActionKind,
        edit_action: VoiceAdminActionKind | None,
        remove_action: VoiceAdminActionKind,
        add_label: str = "Add",
        edit_label: str = "Edit",
        remove_label: str = "Remove",
        item_description: Callable[[ValueT], str],
        item_label: Callable[[str, ValueT], str] = lambda source, _value: source,
    ) -> None:
        paged = _paginate(list(items.items()), state.page)
        selected_value = items.get(selected_key) if selected_key is not None else None
        embed.add_field(
            name=section_title,
            value=_display_value(
                (
                    *summary_lines,
                    f"entries: {len(items)}",
                    (
                        f"selected: {item_label(selected_key, selected_value)}"
                        if selected_key is not None and selected_value is not None
                        else "selected: none"
                    ),
                    (f"value: {item_description(selected_value)}" if selected_value is not None else "value: none"),
                )
            ),
            inline=False,
        )
        if paged.visible:
            layout.add_text_select(
                self._action_codec.build(
                    select_action, page=paged.page_state.page, value=_voice_admin_state_value(state)
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(item_label(source, target)),
                        value=source,
                        description=_component_text(item_description(target)),
                        is_default=source == selected_key,
                    )
                    for source, target in paged.visible
                ],
                placeholder=f"Choose {section_title.lower()}",
            )
        self._add_page_footer(
            layout=layout,
            state=state,
            page_state=paged.page_state,
            extra_buttons=(
                EditorButton(
                    self._action_codec.build(
                        add_action, page=paged.page_state.page, value=_voice_admin_state_value(state)
                    ),
                    add_label,
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                *(
                    (
                        EditorButton(
                            self._action_codec.build(
                                edit_action, page=paged.page_state.page, value=_voice_admin_state_value(state)
                            ),
                            edit_label,
                            style=hikari.ButtonStyle.PRIMARY,
                            is_disabled=selected_value is None,
                        ),
                    )
                    if edit_action is not None
                    else ()
                ),
                EditorButton(
                    self._action_codec.build(
                        remove_action, page=paged.page_state.page, value=_voice_admin_state_value(state)
                    ),
                    remove_label,
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_value is None,
                ),
            ),
        )

    def _render_link_rules_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceAdminState,
        link_rules: Sequence[VoiceLinkRule],
        selected_index: int | None,
        voice_tts: VoiceTTSService,
    ) -> None:
        rule_map = {str(index): rule for index, rule in enumerate(link_rules, start=1)}
        paged = _paginate(list(rule_map.items()), state.page)
        selected_key = str(selected_index) if selected_index is not None else None
        selected_rule = rule_map.get(selected_key) if selected_key is not None else None

        embed.add_field(
            name="Link Rules",
            value=_display_value(
                (
                    f"view: {state.links_view.label}",
                    f"entries: {len(link_rules)}",
                    f"simple rules: {sum(1 for rule in link_rules if rule.mode is VoiceLinkRuleMode.SIMPLE)}",
                    f"regex rules: {sum(1 for rule in link_rules if rule.mode is VoiceLinkRuleMode.REGEX)}",
                    "Add Simple or Add Complex starts from a full example URL.",
                    "template fields: `{title}` is raw and `{title_norm}` is speech-normalized (`_words` also works)",
                )
            ),
            inline=False,
        )
        embed.add_field(
            name="Selected Rule",
            value=_display_value(self._link_rule_detail_lines(selected_key, selected_rule, voice_tts=voice_tts)),
            inline=False,
        )

        if paged.visible:
            embed.add_field(
                name=f"Current Page ({paged.total_count})",
                value=_display_value(
                    [
                        f"{source}. {rule.host} ({rule.mode.value}): {_component_text(_voice_link_rule_value(rule), limit=120)}"
                        for source, rule in paged.visible
                    ]
                ),
                inline=False,
            )
            layout.add_text_select(
                self._action_codec.build(
                    VoiceAdminActionKind.SELECT_LINK_RULE,
                    page=paged.page_state.page,
                    value=_voice_admin_state_value(state),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(f"{source}. {rule.host} ({rule.mode.value})"),
                        value=source,
                        description=_component_text(_voice_link_rule_value(rule)),
                        is_default=source == selected_key,
                    )
                    for source, rule in paged.visible
                ],
                placeholder="Choose link rules",
            )

        self._add_page_footer(
            layout=layout,
            state=state,
            page_state=paged.page_state,
            extra_buttons=(
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.ADD_SIMPLE_LINK_RULE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Add Simple",
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.ADD_COMPLEX_LINK_RULE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Add Complex",
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.EDIT_LINK_RULE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_rule is None,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.REMOVE_LINK_RULE,
                        page=paged.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_rule is None,
                ),
            ),
        )

    def _link_rule_detail_lines(
        self,
        selected_key: str | None,
        selected_rule: VoiceLinkRule | None,
        *,
        voice_tts: VoiceTTSService,
    ) -> Sequence[str]:
        if selected_key is None or selected_rule is None:
            return ("selected: none",)
        preview = voice_tts.preview_voice_link_rule(selected_rule) or "unavailable"
        pattern_label = "shape" if selected_rule.mode is VoiceLinkRuleMode.SIMPLE else "regex"
        return (
            f"selected: {selected_key}. {selected_rule.host} ({selected_rule.mode.value})",
            f"url: {selected_rule.example_url or 'none'}",
            f"{pattern_label}: {selected_rule.input_pattern}",
            f"compiled regex: {selected_rule.path_regex}",
            f"say: {selected_rule.template}",
            f"resolved output: {preview}",
        )

    def _render_global_pronunciations_section(
        self,
        *,
        embed: hikari.Embed,
        layout: EditorLayout,
        state: VoiceAdminState,
        flattened_pronunciations: Sequence[tuple[str, str, PronunciationOverride]],
        global_pronunciations: dict[str, dict[str, PronunciationOverride]],
        selected_value: str | None,
        voice_tts: VoiceTTSService,
    ) -> None:
        paged_pronunciations = _paginate(flattened_pronunciations, state.page)
        selected_entry = (
            next(
                (
                    (voice, source, entry)
                    for voice, source, entry in flattened_pronunciations
                    if _voice_pronunciation_value(voice, source) == selected_value
                ),
                None,
            )
            if selected_value is not None
            else None
        )
        embed.add_field(
            name="Global Pronunciations",
            value=_display_value(
                [
                    f"voices with entries: {len(global_pronunciations)}",
                    f"entries: {len(flattened_pronunciations)}",
                    f"active bot voice ipa: {'available' if voice_tts.voice_supports_ipa_pronunciations(voice_tts.voice) else 'unavailable'}",
                    (
                        f"selected: {selected_entry[0]} / {selected_entry[1]}"
                        if selected_entry is not None
                        else "selected: none"
                    ),
                    (
                        f"value: {_pronunciation_override_display(selected_entry[2])}"
                        if selected_entry is not None
                        else "value: none"
                    ),
                ]
            ),
            inline=False,
        )
        if paged_pronunciations.visible:
            layout.add_text_select(
                self._action_codec.build(
                    VoiceAdminActionKind.SELECT_PRONUNCIATION,
                    page=paged_pronunciations.page_state.page,
                    value=_voice_admin_state_value(state),
                ),
                options=[
                    EditorSelectOption(
                        label=_component_text(f"{voice} / {source}"),
                        value=_voice_pronunciation_value(voice, source),
                        description=_component_text(_pronunciation_override_display(entry)),
                        is_default=_voice_pronunciation_value(voice, source) == selected_value,
                    )
                    for voice, source, entry in paged_pronunciations.visible
                ],
                placeholder="Choose a global pronunciation",
            )
        self._add_page_footer(
            layout=layout,
            state=state,
            page_state=paged_pronunciations.page_state,
            extra_buttons=(
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.SHOW_PRONUNCIATION_CREATE,
                        page=paged_pronunciations.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Add",
                    style=hikari.ButtonStyle.PRIMARY,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.EDIT_PRONUNCIATION,
                        page=paged_pronunciations.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Edit",
                    style=hikari.ButtonStyle.PRIMARY,
                    is_disabled=selected_entry is None,
                ),
                EditorButton(
                    self._action_codec.build(
                        VoiceAdminActionKind.REMOVE_PRONUNCIATION,
                        page=paged_pronunciations.page_state.page,
                        value=_voice_admin_state_value(state),
                    ),
                    "Remove",
                    style=hikari.ButtonStyle.DANGER,
                    is_disabled=selected_entry is None,
                ),
            ),
        )

    def _add_section_selector(self, *, layout: EditorLayout, state: VoiceAdminState) -> None:
        layout.add_text_select(
            self._build_state_action(VoiceAdminActionKind.SHOW_SECTION, state),
            options=[
                EditorSelectOption(
                    label=label,
                    value=section.value,
                    description=f"Open {label.lower()}",
                    is_default=state.section is section,
                )
                for section, label in (
                    (VoiceAdminSection.OVERVIEW, "Overview"),
                    (VoiceAdminSection.CHANNELS, "Channels"),
                    (VoiceAdminSection.MODELS, "Voices"),
                    (VoiceAdminSection.MENTIONS, "Mentions"),
                    (VoiceAdminSection.SUBSTITUTIONS, "Substitutions"),
                    (VoiceAdminSection.PRONUNCIATIONS, "Pronunciations"),
                    (VoiceAdminSection.PROTECTED, "Protect"),
                    (VoiceAdminSection.LINKS, "Links"),
                )
            ],
            placeholder="Choose an admin section",
        )

    def _add_substitution_category_selector(self, *, layout: EditorLayout, state: VoiceAdminState) -> None:
        layout.add_text_select(
            self._build_state_action(VoiceAdminActionKind.SHOW_SUBSTITUTION_CATEGORY, state),
            options=[
                EditorSelectOption(
                    label=category.label,
                    value=category.value,
                    description=f"Show {category.label.lower()} substitutions",
                    is_default=state.substitution_category is category,
                )
                for category in VoiceAdminSubstitutionCategory
            ],
            placeholder="Choose a substitution category",
        )

    def _add_links_view_selector(self, *, layout: EditorLayout, state: VoiceAdminState) -> None:
        layout.add_text_select(
            self._build_state_action(VoiceAdminActionKind.SHOW_LINKS_VIEW, state),
            options=[
                EditorSelectOption(
                    label=view.label,
                    value=view.value,
                    description=f"Show link {view.label.lower()}",
                    is_default=state.links_view is view,
                )
                for view in VoiceAdminLinksView
            ],
            placeholder="Choose a links view",
        )

    def _add_page_footer(
        self,
        *,
        layout: EditorLayout,
        state: VoiceAdminState,
        page_state: EditorPageState,
        extra_buttons: Sequence[EditorButton] = (),
    ) -> None:
        prev_action = None
        next_action = None
        if page_state.total_pages > 1:
            prev_action = self._build_state_action(
                VoiceAdminActionKind.PAGE,
                self._state_with(state, page=max(0, page_state.page - 1)),
            )
            next_action = self._build_state_action(
                VoiceAdminActionKind.PAGE,
                self._state_with(state, page=min(page_state.total_pages - 1, page_state.page + 1)),
            )
        refresh_button = EditorButton(self._build_state_action(VoiceAdminActionKind.REFRESH, state), "Refresh")
        footer_button_count = 1
        if page_state.is_subpage:
            footer_button_count += 1
        if prev_action is not None:
            footer_button_count += 1
        if next_action is not None:
            footer_button_count += 1

        footer_capacity = max(0, 5 - footer_button_count)
        footer_extra_buttons: tuple[EditorButton, ...]
        if len(extra_buttons) + 1 <= footer_capacity:
            footer_extra_buttons = (*extra_buttons, refresh_button)
        else:
            if extra_buttons:
                layout.add_buttons(*extra_buttons)
            footer_extra_buttons = (refresh_button,) if footer_capacity > 0 else ()
        layout.page_footer(
            self._action_codec.build(VoiceAdminActionKind.CLOSE, page=page_state.page),
            page_state=page_state,
            prev_action=prev_action,
            next_action=next_action,
            extra_buttons=footer_extra_buttons,
        )

    def _build_state_action(self, kind: VoiceAdminActionKind, state: VoiceAdminState) -> str:
        return self._action_codec.build(kind, page=state.page, value=_voice_admin_state_value(state))

    def _selection_state_for_message(self, message_id: hikari.Snowflakeish) -> VoiceAdminSelectionState:
        key = hikari.Snowflake(message_id)
        state = self._selection_state.get(key)
        if state is None:
            state = VoiceAdminSelectionState()
            self._selection_state[key] = state
        return state

    def _selection_message_id_from_interaction(self, interaction: hikari.ComponentInteraction) -> hikari.Snowflake:
        message = interaction.message
        if message is None:
            raise ValueError("Editor session is invalid. Reopen the editor and try again.")
        return hikari.Snowflake(message.id)

    @staticmethod
    def _selection_message_id_from_extra(extra: str | None) -> hikari.Snowflake:
        if extra is None:
            raise ValueError("Editor session is invalid. Reopen the editor and try again.")
        return _parse_message_id(extra)

    @staticmethod
    def _state_with(
        state: VoiceAdminState,
        *,
        section: VoiceAdminSection | None = None,
        page: int | None = None,
        substitution_category: VoiceAdminSubstitutionCategory | None = None,
        links_view: VoiceAdminLinksView | None = None,
        pronunciation_view: VoiceAdminPronunciationView | None = None,
        channels_view: VoiceAdminChannelsView | None = None,
    ) -> VoiceAdminState:
        return VoiceAdminState(
            section=state.section if section is None else section,
            page=state.page if page is None else page,
            substitution_category=(
                state.substitution_category if substitution_category is None else substitution_category
            ),
            links_view=state.links_view if links_view is None else links_view,
            pronunciation_view=state.pronunciation_view if pronunciation_view is None else pronunciation_view,
            channels_view=state.channels_view if channels_view is None else channels_view,
        )

    @staticmethod
    def _state_and_extra_from_value(action: object) -> tuple[VoiceAdminState | None, str | None]:
        page = getattr(action, "page", None)
        raw_value = getattr(action, "value", None)
        if not isinstance(page, int) or not isinstance(raw_value, str):
            return None, None

        parts = raw_value.split(_VOICE_ADMIN_STATE_VALUE_SEPARATOR)
        if state := _voice_admin_state_from_action(action):
            return state, None
        if len(parts) <= 4:
            return None, None
        state_value = _VOICE_ADMIN_STATE_VALUE_SEPARATOR.join(parts[:4])
        extra_value = _VOICE_ADMIN_STATE_VALUE_SEPARATOR.join(parts[4:])
        state_action = type("VoiceAdminStateAction", (), {"page": page, "value": state_value})
        if state := _voice_admin_state_from_action(state_action):
            return state, extra_value
        return None, None

    @staticmethod
    def _require_acl(deps: Mapping[str, object]) -> Access_Control:
        value = deps.get("acl")
        if not isinstance(value, Access_Control):
            raise TypeError("Voice admin editor requires Access_Control")
        return value

    @staticmethod
    def _require_voice_tts(deps: Mapping[str, object]) -> VoiceTTSService:
        value = deps.get("voice_tts")
        if not isinstance(value, VoiceTTSService):
            raise TypeError("Voice admin editor requires VoiceTTSService")
        return value


@group_voice.register
class CMD_VoiceSub(
    lightbulb.SlashCommand,
    name="sub",
    description="Manage your TTS text substitutions",
):
    _MAX_MESSAGE_CHARS = 1850

    source = lightbulb.string(
        "source",
        "Word to replace (leave empty to list)",
        autocomplete=ac_tts_substitution_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "Replacement text (omit to remove source)",
        default=None,
    )
    case_sensitive = lightbulb.boolean(
        "case_sensitive",
        "Whether the source should only match the exact casing",
        default=False,
    )

    @classmethod
    def _chunk_substitution_messages(cls, substitutions: dict[str, TextSubstitutionRule]) -> list[str]:
        header = [f"substitutions: `{len(substitutions)}`"]
        if not substitutions:
            return ["\n".join([*header, "No substitutions set. Example: `/voice sub source:im target:I'm`"])]

        messages: list[str] = []
        current = "\n".join([*header, "source -> target:"])
        for source, rule in substitutions.items():
            line = f"`{_substitution_rule_label(source, rule)}` -> `{rule.target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join(["substitutions (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @staticmethod
    def _build_substitution_text_file(substitutions: Mapping[str, TextSubstitutionRule]) -> bytes:
        lines = [f"base substitutions: {len(substitutions)}", ""]
        if substitutions:
            lines.extend(
                f"{_substitution_rule_label(source, rule)} -> {rule.target}" for source, rule in substitutions.items()
            )
        else:
            lines.append("(none)")
        text = "\n".join(lines) + "\n"
        return text.encode(config.STR_ENCODE, "replace")

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        source_input = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None
        case_sensitive = bool(self.case_sensitive)
        source = _normalise_voice_source(source_input)

        log.info(
            f"Voice cmd sub invoked user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
            f"target={voice_tts._preview(target or '')!r} case_sensitive={case_sensitive}"
        )

        if not source.key and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd sub rejected missing_source user={ctx.user.id}")
            return

        if not source.key:
            substitutions = voice_tts.user_text_substitutions(ctx.user.id)
            for message in self._chunk_substitution_messages(substitutions):
                await ctx.respond(message)
            base_substitutions = voice_tts.base_text_substitutions()
            base_file = hikari.Bytes(
                self._build_substitution_text_file(base_substitutions),
                "voice_base_substitutions.txt",
            )
            await ctx.respond(
                f"Attached base substitutions file (`{len(base_substitutions)}` entries).",
                attachment=base_file,
            )
            log.info(f"Voice cmd sub list user={ctx.user.id} count={len(substitutions)}")
            return

        if target is None:
            try:
                source_key, removed = voice_tts.remove_user_text_substitution(ctx.user.id, source.key)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(
                    f"Voice cmd sub rejected remove user={ctx.user.id} source={source.raw!r} "
                    f"source_key={source.key!r} reason={xcp}"
                )
                return

            source_display = source.display() if source.is_emoji else f"`{source_key}`"
            if removed:
                await ctx.respond(f"Removed substitution: {source_display}")
            else:
                await ctx.respond(f"No substitution set for {source_display}.")
            log.info(f"Voice cmd sub remove user={ctx.user.id} source={source_key!r} removed={removed}")
            return

        try:
            source_key, replacement, existed = voice_tts.set_user_text_substitution(
                ctx.user.id,
                source.key,
                target,
                case_sensitive=case_sensitive,
            )
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd sub rejected set user={ctx.user.id} source={source.raw!r} source_key={source.key!r} "
                f"target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        source_display = source.display() if source.is_emoji else f"`{source_key}`"
        await ctx.respond(
            f"{action} substitution: {source_display} ({_substitution_mode_label(replacement.case_sensitive)}) "
            f"-> `{replacement.target}`"
        )
        log.info(
            f"Voice cmd sub set user={ctx.user.id} source={source_key!r} "
            f"replacement={voice_tts._preview(replacement.target)!r} case_sensitive={replacement.case_sensitive} "
            f"updated={existed}"
        )


@group_voice.register
class CMD_VoiceSettings(
    lightbulb.SlashCommand,
    name="settings",
    description="Open the voice settings editor",
):
    public = lightbulb.boolean("public", "Send the editor as a normal message", default=False)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        voice_editor: VoiceSettingsEditorService,
        voice_tts: VoiceTTSService,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.user)
        await voice_editor.open_editor(ctx=ctx, voice_tts=voice_tts, is_public=self.public)


@group_voice.register
class CMD_VoiceAdmin(
    lightbulb.SlashCommand,
    name="admin",
    description="Open the voice admin editor",
):
    public = lightbulb.boolean("public", "Send the editor as a normal message", default=False)  # type: ignore[reportAssignmentType]

    @lightbulb.invoke
    async def invoke(
        self,
        ctx: lightbulb.Context,
        acl: Access_Control,
        voice_admin_editor: VoiceAdminEditorService,
        voice_tts: VoiceTTSService,
    ) -> None:
        await acl.perm_check(ctx.user.id, acl.LvL.admin)
        await voice_admin_editor.open_editor(ctx=ctx, voice_tts=voice_tts, is_public=self.public)


@group_voice.register
class CMD_VoiceGlobalSub(
    lightbulb.SlashCommand,
    name="globalsub",
    description="Manage shared TTS substitutions",
):
    _MAX_MESSAGE_CHARS = 1850

    category = lightbulb.string(
        "category",
        "Shared substitution category",
        choices=[lightbulb.Choice("slang", "slang"), lightbulb.Choice("typo", "typo")],
        default="slang",
    )
    source = lightbulb.string(
        "source",
        "Token to replace (leave empty to list)",
        autocomplete=ac_tts_global_substitution_sources,  # pyright: ignore[reportArgumentType]
        default=None,
    )
    target = lightbulb.string(
        "target",
        "Replacement text (omit to remove source)",
        default=None,
    )
    case_sensitive = lightbulb.boolean(
        "case_sensitive",
        "Whether the source should only match the exact casing",
        default=False,
    )

    @classmethod
    def _chunk_messages(cls, category: str, substitutions: dict[str, TextSubstitutionRule]) -> list[str]:
        header = [f"global {category}: `{len(substitutions)}`"]
        if not substitutions:
            return ["\n".join([*header, "No shared substitutions set."])]

        messages: list[str] = []
        current = "\n".join([*header, "source -> target:"])
        for source, rule in substitutions.items():
            line = f"`{_substitution_rule_label(source, rule)}` -> `{rule.target}`"
            if len(current) + 1 + len(line) > cls._MAX_MESSAGE_CHARS:
                messages.append(current)
                current = "\n".join([f"global {category} (cont):", line])
            else:
                current = f"{current}\n{line}"

        messages.append(current)
        return messages

    @lightbulb.invoke
    async def invoke(self, ctx: lightbulb.Context, acl: Access_Control, voice_tts: VoiceTTSService):
        await acl.perm_check(ctx.user.id, acl.LvL.admin)
        category = self.category.strip() if isinstance(self.category, str) else "slang"
        source_input = self.source.strip() if isinstance(self.source, str) else ""
        target = self.target.strip() if isinstance(self.target, str) else None
        case_sensitive = bool(self.case_sensitive)
        source = _normalise_voice_source(source_input)

        log.info(
            f"Voice cmd globalsub invoked user={ctx.user.id} category={category!r} "
            f"source={source.raw!r} source_key={source.key!r} target={voice_tts._preview(target or '')!r} "
            f"case_sensitive={case_sensitive}"
        )

        if not source.key and target is not None:
            await ctx.respond("source is required when target is provided")
            log.info(f"Voice cmd globalsub rejected missing_source user={ctx.user.id}")
            return

        if not source.key:
            try:
                substitutions = voice_tts.global_text_substitutions(category)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(f"Voice cmd globalsub rejected list user={ctx.user.id} category={category!r} reason={xcp}")
                return

            for message in self._chunk_messages(category, substitutions):
                await ctx.respond(message)
            log.info(f"Voice cmd globalsub list user={ctx.user.id} category={category!r} count={len(substitutions)}")
            return

        if target is None:
            try:
                category_key, source_key, removed = voice_tts.remove_global_text_substitution(category, source.key)
            except ValueError as xcp:
                await ctx.respond(str(xcp))
                log.info(
                    f"Voice cmd globalsub rejected remove user={ctx.user.id} "
                    f"category={category!r} source={source.raw!r} source_key={source.key!r} reason={xcp}"
                )
                return

            source_display = source.display() if source.is_emoji else f"`{source_key}`"
            if removed:
                await ctx.respond(f"Removed global {category_key}: {source_display}")
            else:
                await ctx.respond(f"No global {category_key} set for {source_display}.")
            log.info(
                f"Voice cmd globalsub remove user={ctx.user.id} category={category_key!r} "
                f"source={source_key!r} removed={removed}"
            )
            return

        try:
            category_key, source_key, replacement, existed = voice_tts.set_global_text_substitution(
                category,
                source.key,
                target,
                case_sensitive=case_sensitive,
            )
        except ValueError as xcp:
            await ctx.respond(str(xcp))
            log.info(
                f"Voice cmd globalsub rejected set user={ctx.user.id} category={category!r} "
                f"source={source.raw!r} source_key={source.key!r} target={voice_tts._preview(target)!r} reason={xcp}"
            )
            return

        action = "Updated" if existed else "Added"
        source_display = source.display() if source.is_emoji else f"`{source_key}`"
        await ctx.respond(
            f"{action} global {category_key}: {source_display} "
            f"({_substitution_mode_label(replacement.case_sensitive)}) -> `{replacement.target}`"
        )
        log.info(
            f"Voice cmd globalsub set user={ctx.user.id} category={category_key!r} "
            f"source={source_key!r} replacement={voice_tts._preview(replacement.target)!r} "
            f"case_sensitive={replacement.case_sensitive} updated={existed}"
        )


__all__ = [
    "CMD_VoiceAdmin",
    "CMD_VoiceGlobalSub",
    "CMD_VoiceSay",
    "CMD_VoiceSettings",
    "CMD_VoiceSub",
    "HFRepoRef",
    "PiperPythonVoiceRuntime",
    "SpeechContent",
    "VoiceJob",
    "VoiceRuntimeResetResult",
    "VoiceAdminEditorService",
    "VoiceSettingsEditorService",
    "VoiceTTSService",
    "ac_tts_global_substitution_sources",
    "ac_tts_pronunciation_sources",
    "ac_tts_substitution_sources",
    "ac_tts_variants",
    "ac_tts_voices",
    "group_voice",
]
# AiviA APasz
