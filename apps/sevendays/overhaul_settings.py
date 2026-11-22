"""Undead Legacy server preferences exposed through the shared Settings editor."""

from apps._settings import (
    BoolSettingSpec,
    ChoiceOption,
    ChoiceSpec,
    IntSettingSpec,
    Setting,
    Setting_Group,
)


MIN_WANDERING_HORDE_ENEMIES = 2
MAX_WANDERING_HORDE_ENEMIES = 64


class SevenDaysSettingGroup(Setting_Group):
    UNDEAD_LEGACY = "Undead Legacy"


def undead_legacy_settings() -> tuple[Setting[int] | Setting[bool], ...]:
    settings: tuple[Setting[int] | Setting[bool], ...] = (
        Setting[int](
            IntSettingSpec(
                ChoiceSpec(
                    ChoiceOption("0", "All recipes"),
                    ChoiceOption("1", "Unknown recipes at the current workstation"),
                    ChoiceOption("2", "Known recipes at the current workstation"),
                )
            ),
            "Recipe Filter",
            "RecipeFilter",
            [],
            default=0,
            desc="Which recipes players can see. Requires a server restart.",
        ),
        Setting[int](
            IntSettingSpec(min_value=1),
            "Wandering Horde Frequency",
            "WanderingHordeFrequency",
            [],
            default=16,
            desc="In-game hours between wandering hordes, before random delay and spawn conditions.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0),
            "Wandering Horde Time Range",
            "WanderingHordeRange",
            [],
            default=8,
            desc="Additional random in-game hours added to the wandering horde interval.",
        ),
        Setting[int](
            IntSettingSpec(min_value=MIN_WANDERING_HORDE_ENEMIES, max_value=MAX_WANDERING_HORDE_ENEMIES),
            "Wandering Horde Enemies",
            "WanderingHordeEnemyCount",
            [],
            default=10,
            desc="Base wandering horde size. Base plus random range must not exceed 64 zombies.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=MAX_WANDERING_HORDE_ENEMIES - MIN_WANDERING_HORDE_ENEMIES),
            "Wandering Horde Enemy Range",
            "WanderingHordeEnemyRange",
            [],
            default=10,
            desc="Additional random zombies. Base plus random range must not exceed 64 zombies.",
        ),
        Setting[int](
            IntSettingSpec(
                ChoiceSpec(
                    ChoiceOption("0", "No stat penalty"),
                    ChoiceOption("1", "Near-death trauma"),
                    ChoiceOption("2", "Trauma and random action-skill loss"),
                    ChoiceOption("3", "Trauma, action-skill loss and partial vitals"),
                )
            ),
            "UL Death Stat Penalty",
            "DeathPenalty2",
            [],
            default=3,
            desc="Undead Legacy's additional death consequences, separate from the base game's Death Penalty.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=100),
            "POI Tier Loot Scaling",
            "POITierLootScale",
            [],
            default=0,
            desc="POI tier influence on loot stage: 0 disables it; 100 uses vanilla scaling.",
        ),
        Setting[bool](
            BoolSettingSpec(),
            "Basic Survival Challenges",
            "BasicSurvivalChallenges",
            [],
            default=True,
            desc="Give new players the tutorial challenges on their first spawn.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=23),
            "Dawn Hour",
            "DawnTime",
            [],
            default=7,
            desc="In-game hour when daylight begins (0–23).",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=24),
            "Dusk Hour",
            "DuskTime",
            [],
            default=20,
            desc="In-game hour when night begins (0–24); 24 is midnight.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=100),
            "Ambient Light Level",
            "AmbientLightLevel",
            [],
            default=40,
            desc="Undead Legacy ambient light level, as a percentage.",
        ),
        Setting[int](
            IntSettingSpec(min_value=-1, allow_negative=True),
            "Brightness Override",
            "OverrideBrightness",
            [],
            default=-1,
            desc="Server brightness override. -1 leaves the client brightness setting in control.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0),
            "Action Skill XP Multiplier",
            "ActionSkillXPMultiplier",
            [],
            default=100,
            desc="Action-skill experience percentage. Independent of the base game's XP Multiplier.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0),
            "Trader Mission Cooldown",
            "TraderMissionCooldown",
            [],
            default=20,
            desc="In-game hours before a trader mission can be offered again.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0, max_value=100),
            "Zombie Rage Chance",
            "AIZombieRageChance",
            [],
            default=25,
            desc="Chance for zombie rage, as a percentage.",
        ),
        Setting[bool](
            BoolSettingSpec(),
            "Heatmap Scout Spawns",
            "AIHeatMapScoutsEnabled",
            [],
            default=True,
            desc="Allow heatmap activity to attract scout zombies.",
        ),
        Setting[int](
            IntSettingSpec(
                ChoiceSpec(
                    ChoiceOption("0", "Disabled"),
                    ChoiceOption("1", "Blood moons only"),
                    ChoiceOption("2", "Any time"),
                )
            ),
            "Zombie Digging",
            "AIZombieDiggingEnabled",
            [],
            default=2,
            desc="When zombies can dig through terrain.",
        ),
        Setting[int](
            IntSettingSpec(min_value=0),
            "Zombie Group Damage Boost",
            "AIGroupDamageBoost",
            [],
            default=20,
            desc="Additional group damage boost, as a percentage.",
        ),
    )
    for setting in settings:
        setting.group = SevenDaysSettingGroup.UNDEAD_LEGACY
    return settings
