from __future__ import annotations

from .runtime_imports import (
    TYPE_CHECKING,
    Button,
    Callable,
    Card,
    ChatEvent,
    Checkbox,
    CodeMirror,
    Column,
    Coroutine,
    Html,
    Input,
    Label,
    Literal,
    MutableMapping,
    ParamSpec,
    Protocol,
    ScrollArea,
    Select,
    Slider,
    Table,
    Timer,
    Tooltip,
    TypeAlias,
    TypeVar,
    Upload,
    cast,
)

if TYPE_CHECKING:
    from nicegui.element import Element
    from nicegui.elements.dialog import Dialog
    from nicegui.elements.grid import Grid
    from nicegui.elements.link import Link
    from nicegui.elements.row import Row
    from nicegui.elements.switch import Switch
    from nicegui.elements.textarea import Textarea
    from nicegui.elements.tabs import Tab, TabPanel, TabPanels, Tabs

RefreshableFunction = TypeVar("RefreshableFunction", bound=Callable[..., object])
RefreshableParams = ParamSpec("RefreshableParams")
RefreshableReturn = TypeVar("RefreshableReturn", covariant=True)
RefreshableValue = TypeVar("RefreshableValue")
ModWebRouteCallable = TypeVar("ModWebRouteCallable", bound=Callable[..., object])
type AsyncRefresh = Callable[[], Coroutine[None, None, None]]
ModWebNotificationType: TypeAlias = Literal["positive", "negative", "warning", "info", "ongoing"]


class ModWebNavigate(Protocol):
    def reload(self) -> None: ...

    def to(self, target: str) -> None: ...


class ModWebRunnerUi(Protocol):
    def run(self, **kwargs: object) -> None: ...


class ModWebFastApiApp(Protocol):
    exception_handlers: MutableMapping[object, Callable[..., object]]

    def middleware(self, middleware_type: str) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...

    def add_middleware(self, middleware_class: type[object], **options: object) -> None: ...

    def exception_handler(self, exception: object) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...

    def on_page_exception(self, handler: Callable[[Exception], object]) -> None: ...

    def on_startup(self, handler: Callable[[], None]) -> None: ...

    def on_shutdown(self, handler: Callable[[], object]) -> None: ...

    def get(self, path: str) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...

    def put(self, path: str) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...

    def post(self, path: str) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...


class ModWebRefreshable(Protocol[RefreshableParams, RefreshableReturn]):
    def __call__(self, *args: RefreshableParams.args, **kwargs: RefreshableParams.kwargs) -> RefreshableReturn: ...

    def refresh(self, *args: RefreshableParams.args, **kwargs: RefreshableParams.kwargs) -> None: ...


class ModWebValueContainer(Protocol):
    @property
    def value(self) -> object: ...


class ModWebEventArgumentsContainer(Protocol):
    @property
    def args(self) -> object: ...


class ModWebUi(Protocol):
    @property
    def navigate(self) -> ModWebNavigate: ...

    def notify(
        self,
        message: str,
        *,
        close_button: bool | str = False,
        multi_line: bool = False,
        type: ModWebNotificationType | None = None,
        timeout: int | None = None,
    ) -> None: ...

    def download(self, src: object, filename: str | None = None, media_type: str = "") -> None: ...

    def add_head_html(self, html: str) -> None: ...

    def run_javascript(self, code: str, *, timeout: float = 1.0) -> object: ...

    def refreshable(
        self, func: Callable[RefreshableParams, RefreshableReturn]
    ) -> ModWebRefreshable[RefreshableParams, RefreshableReturn]: ...

    def card(self, *args: object, **kwargs: object) -> "Card": ...

    def column(self, *args: object, **kwargs: object) -> "Column": ...

    def row(self, *args: object, **kwargs: object) -> "Row": ...

    def scroll_area(self, *args: object, **kwargs: object) -> "ScrollArea": ...

    def grid(self, *args: object, **kwargs: object) -> "Grid": ...

    def label(self, text: str = "", *args: object, **kwargs: object) -> "Label": ...

    def html(self, content: str = "", *args: object, **kwargs: object) -> Html: ...

    def element(self, tag: str = "div", *args: object, **kwargs: object) -> "Element": ...

    def input(self, *args: object, **kwargs: object) -> "Input": ...

    def textarea(self, *args: object, **kwargs: object) -> "Textarea": ...

    def select(self, *args: object, **kwargs: object) -> "Select": ...

    def slider(self, *args: object, **kwargs: object) -> "Slider": ...

    def table(self, *args: object, **kwargs: object) -> "Table": ...

    def pagination(self, *args: object, **kwargs: object) -> "Element": ...

    def checkbox(self, *args: object, **kwargs: object) -> "Checkbox": ...

    def switch(self, *args: object, **kwargs: object) -> "Switch": ...

    def codemirror(self, *args: object, **kwargs: object) -> "CodeMirror": ...

    def dialog(self, *args: object, **kwargs: object) -> "Dialog": ...

    def link(self, *args: object, **kwargs: object) -> "Link": ...

    def button(self, *args: object, **kwargs: object) -> "Button": ...

    def icon(self, *args: object, **kwargs: object) -> "Element": ...

    def tabs(self, *args: object, **kwargs: object) -> "Tabs": ...

    def tab(self, *args: object, **kwargs: object) -> "Tab": ...

    def tab_panels(self, *args: object, **kwargs: object) -> "TabPanels": ...

    def tab_panel(self, *args: object, **kwargs: object) -> "TabPanel": ...

    def tooltip(self, text: str = "", *args: object, **kwargs: object) -> "Tooltip": ...

    def upload(self, *args: object, **kwargs: object) -> "Upload": ...

    def timer(self, *args: object, **kwargs: object) -> "Timer": ...

    def menu(self, *args: object, **kwargs: object) -> "Element": ...

    def context_menu(self, *args: object, **kwargs: object) -> "Element": ...

    def menu_item(self, *args: object, **kwargs: object) -> "Element": ...


class ModWebRouteUi(ModWebUi, Protocol):
    def page(
        self, path: str, *args: object, **kwargs: object
    ) -> Callable[[ModWebRouteCallable], ModWebRouteCallable]: ...

    def run(self, **kwargs: object) -> None: ...


class WebChatRelayPublisher(Protocol):
    async def publish_web_chat(
        self,
        *,
        room_id: str,
        session_id: str,
        author_display_name: str,
        author_id: str | None,
        discord_user_id: int | None,
        content: str,
        reply_to_event_id: str | None = None,
    ) -> ChatEvent: ...

    async def publish_chat_event(self, *, event: ChatEvent) -> ChatEvent: ...


def _cast_mod_web_route_ui(value: object) -> ModWebRouteUi:
    return cast(ModWebRouteUi, value)


def _event_args_as_text(container: ModWebEventArgumentsContainer) -> str:
    return _value_as_text(container)


def _value_as_object(container: object) -> object:
    if hasattr(container, "value"):
        return cast(object, getattr(container, "value"))
    if hasattr(container, "args"):
        return cast(object, getattr(container, "args"))
    raise AttributeError(f"{type(container).__name__!r} does not expose a recognised value payload.")


def _value_as_text(container: object) -> str:
    value = _value_as_object(container)
    return "" if value is None else str(value)


def _value_as_bool(container: object) -> bool:
    value = _value_as_object(container)
    if not isinstance(value, bool):
        raise TypeError(f"{type(container).__name__!r} does not contain a boolean value.")
    return value


__all__: tuple[str, ...] = (
    "AsyncRefresh",
    "ModWebEventArgumentsContainer",
    "ModWebFastApiApp",
    "ModWebNotificationType",
    "ModWebNavigate",
    "ModWebRefreshable",
    "ModWebRouteCallable",
    "ModWebRouteUi",
    "ModWebRunnerUi",
    "ModWebUi",
    "ModWebValueContainer",
    "RefreshableFunction",
    "RefreshableParams",
    "RefreshableReturn",
    "RefreshableValue",
    "WebChatRelayPublisher",
    "_cast_mod_web_route_ui",
    "_event_args_as_text",
    "_value_as_bool",
    "_value_as_object",
    "_value_as_text",
)
